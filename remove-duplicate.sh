#!/bin/bash
# YouTube Iframe Converter without backup
# Usage: ./convert_iframes_nobackup.sh filename.md

if [ $# -ne 1 ]; then
    echo "Usage: $0 <filename>"
    exit 1
fi

FILE="$1"

if [ ! -f "$FILE" ]; then
    echo "❌ File not found: $FILE"
    exit 1
fi

# Use sed to replace YouTube iframes with specific dimensions
# This replaces width="700" or width="720" and height="400" or height="405"
# with responsive format with margin-top: 30px
sed -i -E 's/<iframe([^>]*) width="(700|720)"([^>]*) height="(400|405)"([^>]*src="[^"]*youtube[^"]*"[^>]*)>/<iframe width="100%" height="315" \3 \5 style="margin-top: 30px;">/' "$FILE"

echo "✅ YouTube iframes converted in $FILE"
