#!/usr/bin/env python
# coding: utf-8
# In[2]:


from textwrap import wrap
import os
import keras_cv
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import tensorflow as tf
import tensorflow.experimental.numpy as tnp
from keras_cv.models.stable_diffusion.clip_tokenizer import SimpleTokenizer
from keras_cv.models.stable_diffusion.diffusion_model import DiffusionModel
from keras_cv.models.stable_diffusion.image_encoder import ImageEncoder
from keras_cv.models.stable_diffusion.noise_scheduler import NoiseScheduler
from keras_cv.models.stable_diffusion.text_encoder import TextEncoder
from stable_diffusion_tf.stable_diffusion import StableDiffusion as StableDiffusionPy
from keras_cv.models.stable_diffusion.diffusion_model import DiffusionModel
from tensorflow import keras
from tensorflow.keras.callbacks import TensorBoard

# In[3]:


data_path = "dataset"

data_frame = pd.read_csv(os.path.join(data_path, "data_1.csv"))

data_frame["image"] = data_frame["image"].apply(
    lambda x: os.path.join(data_path, x)
)
data_frame.head()


# In[4]:


print("Num GPUs Available: ", len(tf.config.list_physical_devices('GPU')))
physical_devices = tf.config.list_physical_devices('GPU')
tf.config.experimental.get_device_details(physical_devices[0])


# In[5]:


# Define the Mixed Strategy 
strategy = tf.distribute.MirroredStrategy()


# In[6]:


# The padding token and maximum prompt length are specific to the text encoder.
# If you're using a different text encoder be sure to change them accordingly.
PADDING_TOKEN = 49407
MAX_PROMPT_LENGTH = 77

# Load the tokenizer.
tokenizer = SimpleTokenizer()

#  Method to tokenize and pad the tokens.
def process_text(caption):
    tokens = tokenizer.encode(caption)
    tokens = tokens + [PADDING_TOKEN] * (MAX_PROMPT_LENGTH - len(tokens))
    return np.array(tokens)


# Collate the tokenized captions into an array.
tokenized_texts = np.empty((len(data_frame), MAX_PROMPT_LENGTH))

all_captions = list(data_frame["caption"].values)
for i, caption in enumerate(all_captions):
    tokenized_texts[i] = process_text(caption)


# In[7]:


RESOLUTION = 256
AUTO = tf.data.AUTOTUNE
POS_IDS = tf.convert_to_tensor([list(range(MAX_PROMPT_LENGTH))], dtype=tf.int32)

augmenter = keras.Sequential(
    layers=[
        keras_cv.layers.CenterCrop(RESOLUTION, RESOLUTION),
        keras_cv.layers.RandomFlip(),
        tf.keras.layers.Rescaling(scale=1.0 / 127.5, offset=-1),
    ]
)
text_encoder = TextEncoder(MAX_PROMPT_LENGTH)


def process_image(image_path, tokenized_text):
    image = tf.io.read_file(image_path)
    image = tf.io.decode_png(image, 3)
    image = tf.image.resize(image, (RESOLUTION, RESOLUTION))
    return image, tokenized_text


def apply_augmentation(image_batch, token_batch):
    return augmenter(image_batch), token_batch


def run_text_encoder(image_batch, token_batch):
    return (
        image_batch,
        token_batch,
        text_encoder([token_batch, POS_IDS], training=False),
    )


def prepare_dict(image_batch, token_batch, encoded_text_batch):
    return {
        "images": image_batch,
        "tokens": token_batch,
        "encoded_text": encoded_text_batch,
    }


def prepare_dataset(image_paths, tokenized_texts, batch_size=1):
    dataset = tf.data.Dataset.from_tensor_slices((image_paths, tokenized_texts))
    dataset = dataset.shuffle(batch_size * 10)
    dataset = dataset.map(process_image, num_parallel_calls=AUTO).batch(batch_size)
    dataset = dataset.map(apply_augmentation, num_parallel_calls=AUTO)
    dataset = dataset.map(run_text_encoder, num_parallel_calls=AUTO)
    dataset = dataset.map(prepare_dict, num_parallel_calls=AUTO)
    return dataset.prefetch(AUTO)


# In[8]:


from sklearn.model_selection import train_test_split

image_paths = np.array(data_frame["image"])
tokenized_texts = np.array(tokenized_texts)  # Make sure this is an array

train_images, val_images, train_texts, val_texts = train_test_split(
    image_paths, tokenized_texts, test_size=0.1, random_state=42
)

training_dataset = prepare_dataset(train_images, train_texts, batch_size=6 * strategy.num_replicas_in_sync)
validation_dataset = prepare_dataset(val_images, val_texts, batch_size=6 * strategy.num_replicas_in_sync)

# Check the shapes of a sample batch from the training dataset
sample_train_batch = next(iter(training_dataset))
for k in sample_train_batch:
    print("Training:", k, sample_train_batch[k].shape)

# Check the shapes of a sample batch from the validation dataset
sample_val_batch = next(iter(validation_dataset))
for k in sample_val_batch:
    print("Validation:", k, sample_val_batch[k].shape)

# In[11]:


diffusion_model_pytorch_weights = keras.utils.get_file(
    origin="https://huggingface.co/riffusion/riffusion-model-v1/resolve/main/riffusion-model-v1.ckpt",
    file_hash="99a6eb51c18e16a6121180f3daa69344e571618b195533f67ae94be4eb135a57",
)


# In[14]:


with strategy.scope():
    diffusion_model=StableDiffusionPy(RESOLUTION, RESOLUTION, download_weights=False)
    diffusion_model.load_weights_from_pytorch_ckpt(diffusion_model_pytorch_weights)


# In[15]:


class Trainer(tf.keras.Model):
    # Reference:
    # https://github.com/huggingface/diffusers/blob/main/examples/text_to_image/train_text_to_image.py

    def __init__(
        self,
        diffusion_model,
        vae,
        noise_scheduler,
        use_mixed_precision=False,
        max_grad_norm=1.0,
        **kwargs
    ):
        super().__init__(**kwargs)

        self.diffusion_model = diffusion_model
        self.vae = vae
        self.noise_scheduler = noise_scheduler
        self.max_grad_norm = max_grad_norm

        self.use_mixed_precision = use_mixed_precision
        self.vae.trainable = False

    def train_step(self, inputs):
        images = inputs["images"]
        encoded_text = inputs["encoded_text"]
        batch_size = tf.shape(images)[0]

        with tf.GradientTape() as tape:
            # Project image into the latent space and sample from it.
            latents = self.sample_from_encoder_outputs(self.vae(images, training=False))
            # Know more about the magic number here:
            # https://keras.io/examples/generative/fine_tune_via_textual_inversion/
            latents = latents * 0.18215

            # Sample noise that we'll add to the latents.
            noise = tf.random.normal(tf.shape(latents))

            # Sample a random timestep for each image.
            timesteps = tnp.random.randint(
                0, self.noise_scheduler.train_timesteps, (batch_size,)
            )

            # Add noise to the latents according to the noise magnitude at each timestep
            # (this is the forward diffusion process).
            noisy_latents = self.noise_scheduler.add_noise(
                tf.cast(latents, noise.dtype), noise, timesteps
            )

            # Get the target for loss depending on the prediction type
            # just the sampled noise for now.
            target = noise  # noise_schedule.predict_epsilon == True

            # Predict the noise residual and compute loss.
            timestep_embedding = tf.map_fn(
                lambda t: self.get_timestep_embedding(t), timesteps, fn_output_signature=tf.float32
            )
            timestep_embedding = tf.squeeze(timestep_embedding, 1)
            model_pred = self.diffusion_model(
                [noisy_latents, timestep_embedding, encoded_text], training=True
            )
            loss = self.compiled_loss(target, model_pred)
            if self.use_mixed_precision:
                loss = self.optimizer.get_scaled_loss(loss)

        # Update parameters of the diffusion model.
        trainable_vars = self.diffusion_model.trainable_variables
        gradients = tape.gradient(loss, trainable_vars)
        if self.use_mixed_precision:
            gradients = self.optimizer.get_unscaled_gradients(gradients)
        gradients = [tf.clip_by_norm(g, self.max_grad_norm) for g in gradients]
        self.optimizer.apply_gradients(zip(gradients, trainable_vars))

        return {m.name: m.result() for m in self.metrics}
    
    def test_step(self, inputs):
        images = inputs["images"]
        encoded_text = inputs["encoded_text"]
        batch_size = tf.shape(images)[0]

        latents = self.sample_from_encoder_outputs(self.vae(images, training=False))
        latents = latents * 0.18215
        noise = tf.random.normal(tf.shape(latents))
        timesteps = tnp.random.randint(0, self.noise_scheduler.train_timesteps, (batch_size,))
        noisy_latents = self.noise_scheduler.add_noise(tf.cast(latents, noise.dtype), noise, timesteps)
        target = noise
        timestep_embedding = tf.map_fn(lambda t: self.get_timestep_embedding(t), timesteps, fn_output_signature=tf.float32)
        timestep_embedding = tf.squeeze(timestep_embedding, 1)
        model_pred = self.diffusion_model([noisy_latents, timestep_embedding, encoded_text], training=False)
    
        # Use compiled_loss
        loss = self.compiled_loss(target, model_pred)
    
        return {'loss': loss}

    def get_timestep_embedding(self, timestep, dim=320, max_period=10000):
        half = dim // 2
        log_max_preiod = tf.math.log(tf.cast(max_period, tf.float32))
        freqs = tf.math.exp(
            -log_max_preiod * tf.range(0, half, dtype=tf.float32) / half
        )
        args = tf.convert_to_tensor([timestep], dtype=tf.float32) * freqs
        embedding = tf.concat([tf.math.cos(args), tf.math.sin(args)], 0)
        embedding = tf.reshape(embedding, [1, -1])
        return embedding

    def sample_from_encoder_outputs(self, outputs):
        mean, logvar = tf.split(outputs, 2, axis=-1)
        logvar = tf.clip_by_value(logvar, -30.0, 20.0)
        std = tf.exp(0.5 * logvar)
        sample = tf.random.normal(tf.shape(mean), dtype=mean.dtype)
        return mean + std * sample

    def save_weights(self, filepath, overwrite=True, save_format=None, options=None):
        # Overriding this method will allow us to use the `ModelCheckpoint`
        # callback directly with this trainer class. In this case, it will
        # only checkpoint the `diffusion_model` since that's what we're training
        # during fine-tuning.
        self.diffusion_model.save_weights(
            filepath=filepath,
            overwrite=overwrite,
            save_format=save_format,
            options=options,
        )


# In[16]:


# Enable mixed-precision training if the underlying GPU has tensor cores.
USE_MP = True
if USE_MP:
  keras.mixed_precision.set_global_policy("mixed_float16")

with strategy.scope():
    
    image_encoder = ImageEncoder()
    diffusion_ft_trainer = Trainer(
        diffusion_model=diffusion_model.diffusion_model,
        # Remove the top layer from the encoder, which cuts off the variance and only
        # returns the mean.
        vae=tf.keras.Model(
            image_encoder.input,
            image_encoder.layers[-2].output,
        ),
        noise_scheduler=NoiseScheduler(),
        use_mixed_precision=USE_MP,
    )

    # These hyperparameters come from this tutorial by Hugging Face:
    # https://huggingface.co/docs/diffusers/training/text2image
    lr = 1e-5
    beta_1, beta_2 = 0.9, 0.999
    weight_decay = (1e-2,)
    epsilon = 1e-08

    optimizer = tf.keras.optimizers.experimental.AdamW(
        learning_rate=lr,
        weight_decay=weight_decay,
        beta_1=beta_1,
        beta_2=beta_2,
        epsilon=epsilon,
    )
    diffusion_ft_trainer.compile(optimizer=optimizer, loss="mse")

# In[ ]:


log_dir = "logs/fit"
tensorboard_callback = TensorBoard(log_dir=log_dir, histogram_freq=1)

total_epochs = 20
ckpt_path = "checkpoints/finetuned_riffusion_itt_s20.h5"
ckpt_callback = tf.keras.callbacks.ModelCheckpoint(
    ckpt_path,
    save_weights_only=True,
    monitor="val_loss",  # Monitor validation loss now
    mode="min",
)

callbacks = [ckpt_callback, tensorboard_callback]
train_losses=[]
val_losses=[]

for epoch in range(total_epochs):
    # Train for one epoch and validate
    history = diffusion_ft_trainer.fit(training_dataset, validation_data=validation_dataset, epochs=1, callbacks=callbacks)
    
    # Print training and validation losses
    train_loss = history.history['loss'][0]
    val_loss = history.history['val_loss'][0]
    print(f"Epoch {epoch + 1}: Training Loss = {train_loss}, Validation Loss = {val_loss}")
    train_losses.append(train_loss)
    val_losses.append(val_loss)


# In[ ]:

# Set up a style - ggplot gives a nice aesthetic touch
plt.style.use('ggplot')

fig, ax = plt.subplots(figsize=(10, 6))  # Bigger size for clarity

# Plotting the training and validation losses
ax.plot(train_losses, 'b', linestyle='-', linewidth=2, label='Training Loss')
ax.plot(val_losses, 'r', linestyle='--', linewidth=2, label='Validation Loss')

# Titles, labels, and legend
ax.set_title('Loss Curve Over Epochs', fontsize=16, fontweight='bold')
ax.set_xlabel('Epochs', fontsize=14)
ax.set_ylabel('Loss', fontsize=14)
ax.legend(loc='upper right', fontsize=12)

# Displaying gridlines
ax.grid(True, linestyle='--')

plt.tight_layout()  # Adjusts subplot params for better layout
plt.show()

fig.savefig('loss_curve.png', dpi=300, bbox_inches='tight')



# In[ ]:




