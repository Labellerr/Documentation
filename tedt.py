import re
import os
import sys

def convert_youtube_embeds(content):
    """
    Convert all YouTube iframe embeds to Mintlify-compatible format.
    
    Args:
        content (str): The document content containing YouTube embeds
        
    Returns:
        str: The document content with converted YouTube embeds
    """
    
    # Pattern to match YouTube iframe embeds
    # This pattern matches various YouTube URL formats and extracts the video ID
    youtube_pattern = r'<iframe[^>]*src="https?://(?:www\.)?youtube\.com/embed/([a-zA-Z0-9_-]{11})[^"]*"[^>]*title="([^"]*)"[^>]*>.*?</iframe>'
    
    def replace_embed(match):
        video_id = match.group(1)
        title = match.group(2)
        
        # Return the new format
        return f'''<iframe 
  className="w-full aspect-video rounded-xl" 
  src="https://www.youtube.com/embed/{video_id}" 
  title="{title}" 
  frameborder="0" 
  allowfullscreen>
</iframe>'''
    
    # Replace all YouTube embeds
    converted_content = re.sub(youtube_pattern, replace_embed, content, flags=re.DOTALL)
    
    return converted_content

def process_file(file_path):
    """
    Process a single file, converting YouTube embeds.
    
    Args:
        file_path (str): Path to the file to process
        
    Returns:
        bool: True if the file was modified, False otherwise
    """
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Convert YouTube embeds
        converted_content = convert_youtube_embeds(content)
        
        # Only write back if content was changed
        if content != converted_content:
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(converted_content)
            return True
        return False
    except Exception as e:
        print(f"Error processing {file_path}: {e}")
        return False

def process_directory(directory_path):
    """
    Process all MDX files in a directory and its subdirectories.
    
    Args:
        directory_path (str): Path to the directory to process
        
    Returns:
        tuple: (number of files processed, number of files modified)
    """
    files_processed = 0
    files_modified = 0
    
    # Walk through all files in the directory and subdirectories
    for root, dirs, files in os.walk(directory_path):
        for file in files:
            # Check if file is an MDX file
            if file.lower().endswith('.mdx'):
                file_path = os.path.join(root, file)
                files_processed += 1
                
                if process_file(file_path):
                    files_modified += 1
                    print(f"Modified: {file_path}")
                else:
                    print(f"No changes needed: {file_path}")
    
    return files_processed, files_modified

def main():
    # Check if directory path is provided
    if len(sys.argv) < 2:
        print("Usage: python convert_youtube_embed.py <directory_path>")
        sys.exit(1)
    
    directory_path = sys.argv[1]
    
    # Check if directory exists
    if not os.path.isdir(directory_path):
        print(f"Error: Directory '{directory_path}' does not exist.")
        sys.exit(1)
    
    print(f"Processing MDX files in {directory_path} and subdirectories...")
    
    # Process all MDX files in the directory
    files_processed, files_modified = process_directory(directory_path)
    
    print(f"\nSummary:")
    print(f"Files processed: {files_processed}")
    print(f"Files modified: {files_modified}")

if __name__ == "__main__":
    main()