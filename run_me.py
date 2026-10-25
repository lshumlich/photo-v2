
import create_viewer
import extract_locations
import rename_photos

# dir = r"C:\$temp\2025-12 Australia 11-17 to 12-15"
dir = r"C:\$temp\temp"

print(f"Folder: {dir}")

# rename_photos.main([str(dir)])
rename_photos.main([str(dir), str('--apply')])
extract_locations.main([str(dir)])
create_viewer.main([str(dir)])


print("Done")

