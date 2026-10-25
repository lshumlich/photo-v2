# Photo Rename and Map

This system has three steps:

1. Walks a photo and video directory tree, reads each file’s capture date/time, and prepends `yyyymmdd_hhmm_nnn` plus a space to the existing name (`IMG_1234.jpg` → `20230515_1430_001 IMG_1234.jpg`). Files with no capture timestamp are left alone. Files that already have that prefix are skipped, so the rename can be run again with the same result.
1. Walks a photo and video directory tree, reads each file’s date/time and location and stores it in a meta directory
1. Creates an html based viewer to see the photos and videos on a map or in date order.

Supported video extensions: `.mov`, `.mp4`.

## Install

The following explains virtual environments quit well: [Python Environments](https://python.land/virtual-environments/virtualenv)


Once you have an active environment the pip commands work on that environment.

| Command                                 | Action                         |
| --------------------------------------- | ------------------------------ |
| python -m venv .lfs_env                 | create environment             |
| .lfs_env\Scripts\activate               | activate environment           |
| pip install -r requirements.txt         | as it says                     |
| pip install openpyxl                    | install package in environment |
| deactivate                              | exit environment               |
| pip list                                | List packages in environment   |
| python.exe -m pip install --upgrade pip | upgrade pip                    |
| From VSCode: Ctrl+Shift+P               | Python: Select Interpreter     |
| pip freeze > requirements.txt           | save the environment           |
| rm -r venv                              | remove the environment         |


## Usage

```dos

.lfs_env\Scripts\activate 

#
# The rename step - --dry-run is the default of not specified
#

python rename_photos.py --dry-run 'C:\path\to\photos'
python rename_photos.py --apply 'C:\path\to\photos'

#
# Extract locations and update meta data
#

python extract_locations.py 'H:\pictures\2026\2026-09 Bike Trip Europe'

#
# Serve viewer
#

python create_viewer.py 'H:\pictures\2026\2026-09 Bike Trip Europe'

#
# Or run one, two, or all three steps together
#    This option requirs you to edit the py file first 
#    to set appropriate options. See file first
#

python run_me.py "C:\path\to\photos" 

```

Then either open `$Photos.html`, or use `$Open-Photos.cmd` if tiles are blocked.

- Map view uses circular photo pins and clusters overlapping shots.
- Gallery groups thumbnails by capture date.
- `create_viewer.py` writes `meta/thumbs.zip` plus `$thumbs.js` so map/gallery thumbs work when you open `$Photos.html` directly. The lightbox still opens the original file.
- Items without GPS appear in the gallery only.
- Re-run `create_viewer.py` after `extract_locations.py` so new files show up.

# Summary


```dos

.lfs_env\Scripts\activate 
python rename_photos.py 'c:\$temp\2026-09 Bike Trip Europe' --apply

python extract_locations.py 'c:\$temp\2026-09 Bike Trip Europe'
python create_viewer.py 'c:\$temp\2026-09 Bike Trip Europe'

```