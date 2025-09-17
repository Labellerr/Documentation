#!/usr/bin/env python3
"""
Convert Responsive Vimeo Embeds to Simple iframes
=================================================
Converts complex responsive Vimeo embed code to simple iframe tags.

Usage:
    python replace.py --dir ./actions/
    python replace.py --input file.mdx --output fixed_file.mdx
"""

import re
import os
import argparse
from typing import List

class VimeoEmbedConverter:
    
    def __init__(self, width: int = 720, height: int = 405):
        self.width = width
        self.height = height
        
        # Pattern to match responsive Vimeo embeds
        self.responsive_pattern = re.compile(
            r'<div\s+style="padding:56\.25%\s+0\s+0\s+0;position:relative;">\s*'
            r'<iframe\s+[^>]*src="https://player\.vimeo\.com/video/(\d+)[^"]*"[^>]*>\s*'
            r'</iframe>\s*'
            r'</div>',
            re.DOTALL | re.IGNORECASE
        )
        
        # Alternative pattern for variations
        self.responsive_pattern_alt = re.compile(
            r'<div[^>]*padding:\s*56\.25%[^>]*>\s*'
            r'<iframe[^>]*src="https://player\.vimeo\.com/video/(\d+)[^"]*"[^>]*>\s*'
            r'</iframe>\s*'
            r'</div>',
            re.DOTALL | re.IGNORECASE
        )
    
    def extract_video_id(self, embed_code: str) -> str:
        """Extract Vimeo video ID from embed code"""
        # Try main pattern first
        match = self.responsive_pattern.search(embed_code)
        if match:
            return match.group(1)
        
        # Try alternative pattern
        match = self.responsive_pattern_alt.search(embed_code)
        if match:
            return match.group(1)
        
        # Try simple regex for video ID
        id_match = re.search(r'vimeo\.com/video/(\d+)', embed_code)
        if id_match:
            return id_match.group(1)
        
        return ""
    
    def create_simple_iframe(self, video_id: str) -> str:
        """Create simple iframe embed code"""
        return f'''<iframe 
  src="https://player.vimeo.com/video/{video_id}" 
  width="{self.width}" 
  height="{self.height}" 
  frameborder="0" 
  allow="autoplay; fullscreen; picture-in-picture" 
  allowfullscreen>
</iframe>'''
    
    def convert_embeds_in_text(self, text: str) -> tuple[str, int]:
        """Convert all responsive Vimeo embeds in text to simple iframes"""
        conversions = 0
        
        def replace_embed(match):
            nonlocal conversions
            video_id = match.group(1)
            conversions += 1
            print(f"  ✅ Converting Vimeo video {video_id}")
            return self.create_simple_iframe(video_id)
        
        # Convert main pattern
        text = self.responsive_pattern.sub(replace_embed, text)
        
        # Convert alternative pattern
        text = self.responsive_pattern_alt.sub(replace_embed, text)
        
        return text, conversions
    
    def process_file(self, input_file: str, output_file: str = None) -> bool:
        """Process a single file"""
        if not output_file:
            output_file = input_file  # Overwrite original
        
        try:
            print(f"📄 Processing {input_file}...")
            
            with open(input_file, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # Convert embeds
            converted_content, conversions = self.convert_embeds_in_text(content)
            
            if conversions > 0:
                # Save converted content
                with open(output_file, 'w', encoding='utf-8') as f:
                    f.write(converted_content)
                
                print(f"  🎉 Converted {conversions} Vimeo embeds")
                print(f"  💾 Updated {output_file}")
                return True
            else:
                print("  ℹ️  No Vimeo embeds found to convert")
                return False
                
        except FileNotFoundError:
            print(f"  ❌ File not found: {input_file}")
            return False
        except Exception as e:
            print(f"  ❌ Error processing {input_file}: {e}")
            return False
    
    def process_directory(self, directory: str) -> int:
        """Process all markdown files in a directory"""
        total_files_converted = 0
        total_embeds_converted = 0
        
        if not os.path.isdir(directory):
            print(f"❌ Directory not found: {directory}")
            return 0
        
        print(f"📂 Processing directory: {directory}")
        
        # Find all markdown files
        md_files = []
        for root, dirs, files in os.walk(directory):
            for file in files:
                if file.endswith(('.md', '.mdx')):
                    md_files.append(os.path.join(root, file))
        
        if not md_files:
            print("  ℹ️  No markdown files found")
            return 0
        
        print(f"  📋 Found {len(md_files)} markdown files")
        
        # Process each file
        for md_file in md_files:
            success = self.process_file(md_file, md_file)  # Overwrite original
            if success:
                total_files_converted += 1
        
        print(f"\n📊 Summary:")
        print(f"  Files processed: {len(md_files)}")
        print(f"  Files with conversions: {total_files_converted}")
        
        return total_files_converted

def main():
    parser = argparse.ArgumentParser(description='Convert responsive Vimeo embeds to simple iframes')
    parser.add_argument('--input', '-i', help='Input markdown file')
    parser.add_argument('--output', '-o', help='Output markdown file (optional)')
    parser.add_argument('--dir', '-d', help='Process all markdown files in directory')
    parser.add_argument('--width', '-w', type=int, default=720, help='iframe width (default: 720)')
    parser.add_argument('--height', '--ht', type=int, default=405, help='iframe height (default: 405)')  # Fixed conflict
    
    args = parser.parse_args()
    
    print("🎬 Vimeo Embed Converter")
    print("=" * 25)
    
    converter = VimeoEmbedConverter(width=args.width, height=args.height)
    
    if args.dir:
        # Process directory
        conversions = converter.process_directory(args.dir)
        if conversions > 0:
            print(f"\n🎉 Successfully processed {conversions} files!")
        else:
            print("\n ℹ️ No conversions needed")
            
    elif args.input:
        # Process single file
        success = converter.process_file(args.input, args.output)
        if success:
            print("\n🎉 Conversion completed!")
        else:
            print("\n ℹ️ No conversion needed")
    else:
        print("❌ Please specify either --dir or --input")
        print("Examples:")
        print("  python replace.py --dir ./actions/")
        print("  python replace.py --input file.mdx")

if __name__ == "__main__":
    main()
