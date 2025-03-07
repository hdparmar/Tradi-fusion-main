

import os
import pandas as pd
#rom datasets import Dataset, load_dataset
#from PIL import Image


# Path to the Spectrograms Dataset
spectrogram_dir = 'new_itt_spec_3c'

# Get a list of all the spectrogram files in the directory
spectrogram_files = [f for f in os.listdir(spectrogram_dir) if f.endswith('.png')] # Adjust the extension if needed

# Create a list of the same length with the caption for each spectrogram
captions = ['An Irish Traditional Tune' for _ in spectrogram_files]

# Create a DataFrame
df = pd.DataFrame({
    'file_name': spectrogram_files,
    'text': captions,
})

# Save the DataFrame as a CSV file
df.to_csv('metadata.csv', index=False)





"""
# Create a DataFrame
df = pd.DataFrame({
    'image_path': spectrogram_files,
    'caption': captions,
})

# Convert DataFrame to a HuggingFace Dataset
dataset = Dataset.from_pandas(df)

# Check first example
print(dataset[0])

# Load images
# dataset = dataset.map(lambda example: {'image': Image.open(example['spectrogram_files'])}, remove_columns=['spectrogram_files'])

# Push the dataset to the Hub under your namespace
dataset.push_to_hub("hdparmar/specdata")
"""