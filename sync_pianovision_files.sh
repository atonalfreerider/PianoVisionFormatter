#!/bin/bash
# filepath: /home/john/Desktop/Piano/PianoVisionFormatter/sync_pianovision_files.sh

# --- Configuration ---
TARGET_DIR="/sdcard/Android/data/com.ZarApps.PianoVision/files" # Standard ADB path
FILE_PATTERN="*.json"

# Check if a source directory is provided as an argument
if [ -n "$1" ]; then
    SOURCE_DIR="$1"
    echo "Using provided source directory: $SOURCE_DIR"
else
    echo "No source directory provided"
    exit 1
fi

# Validate if SOURCE_DIR exists
if [ ! -d "$SOURCE_DIR" ]; then
    echo "Error: Source directory '$SOURCE_DIR' not found."
    exit 1
fi
# --- End Configuration ---

echo "Starting PianoVision JSON sync..."

# --- Check for ADB and connected device ---
echo "Checking for ADB devices..."
DEVICE_ID=$(adb devices | awk 'NR>1 && $2=="device" {print $1; exit}')

if [ -z "$DEVICE_ID" ]; then
    echo "Error: No ADB device found or device not authorized."
    echo "Please ensure a device is connected, USB debugging is enabled, and authorized."
    exit 1
fi
echo "Found device: $DEVICE_ID"

# --- Ensure target directory exists (optional, adb push might create it) ---
adb -s "$DEVICE_ID" shell "mkdir -p \"$TARGET_DIR\""
if [ $? -ne 0 ]; then
    echo "Warning: Could not ensure target directory exists. Proceeding anyway."
fi

# --- Sync files from Source to Target (Update/Add) ---
echo "Syncing files from $SOURCE_DIR to $TARGET_DIR on device..."
# Use find to handle spaces in filenames and avoid issues if no JSON files exist
find "$SOURCE_DIR" -maxdepth 1 -name "$FILE_PATTERN" -print0 | while IFS= read -r -d $'\0' file; do
    echo "  Pushing $(basename "$file")..."
    adb -s "$DEVICE_ID" push --sync "$file" "$TARGET_DIR/"
    if [ $? -ne 0 ]; then
        echo "  Warning: Failed to push $(basename "$file")"
    fi
done
echo "Push/Update phase complete."

# --- Handle Removals (Remove files on Target not in Source) ---
# The PianoVision app keeps its own JSON data in the same folder
# (e.g. finger_position_recordings.json), so removal is opt-in: pass --delete
# as the second argument. Prefer `python3 -m pianovision deploy --prune`, which
# only removes files it deployed itself.
if [ "$2" != "--delete" ]; then
    echo "Skipping removals (pass --delete as the second argument to remove device files not in the source)."
    echo "Sync finished."
    exit 0
fi
PROTECTED_FILES=("finger_position_recordings.json")
echo "Checking for files to remove from device..."

# Get source filenames (basename only)
source_files=()
while IFS= read -r -d $'\0' file; do
    source_files+=("$(basename "$file")")
done < <(find "$SOURCE_DIR" -maxdepth 1 -name "$FILE_PATTERN" -print0)

# Get target filenames (basename only)
target_files_str=$(adb -s "$DEVICE_ID" shell "ls -p \"$TARGET_DIR\" | grep -v '/$' | grep '\.json$'")
if [ $? -ne 0 ]; then
    echo "Error: Failed to list files in $TARGET_DIR on device."
    exit 1
fi

# Convert target files string to array, handling potential empty list
mapfile -t target_files <<< "$target_files_str"

# Compare and remove
removed_count=0
for target_file in "${target_files[@]}"; do
    # Trim potential carriage returns from adb output
    target_file_clean=$(echo "$target_file" | tr -d '\r')
    if [[ -z "$target_file_clean" ]]; then # Skip empty lines
        continue
    fi

    found=0
    for protected in "${PROTECTED_FILES[@]}"; do
        if [[ "$protected" == "$target_file_clean" ]]; then
            found=1
        fi
    done
    for source_file in "${source_files[@]}"; do
        if [[ "$source_file" == "$target_file_clean" ]]; then
            found=1
            break # Corrected from 'breaks'
        fi
    done

    if [[ $found -eq 0 ]]; then
        echo "  Removing $target_file_clean from device..."
        adb -s "$DEVICE_ID" shell "rm \"$TARGET_DIR/$target_file_clean\""
        if [ $? -eq 0 ]; then
            ((removed_count++))
        else
            echo "  Warning: Failed to remove $target_file_clean"
        fi
    fi
done

echo "Removal phase complete. Removed $removed_count file(s)."
echo "Sync finished."
exit 0