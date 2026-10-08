# Photo geotagger

A single-file web server (`geotag_server.py`, Python 3 standard library only) that lets you
upload photos and GPX tracks from any browser and then geotags the photos with
[exiftool](https://exiftool.org/).

## Requirements

- Python 3.8+
- `exiftool` on the PATH (`sudo apt install libimage-exiftool-perl`, `brew install exiftool`, …)

## Run

```sh
python3 geotag_server.py --port 8000 --dir ./uploads
```

Open `http://<server-ip>:8000/`, drop photos and `.gpx` files onto the page. With
**auto-run after upload** ticked (the default), exiftool runs as soon as the uploads finish;
otherwise press **Geotag photos**.

Files are stored in `<dir>/photos` and `<dir>/gpx`. The command that runs is:

```sh
exiftool -P -geotag a.gpx -geotag b.gpx ['-geotime<${DateTimeOriginal}+TZ'] [-geosync=OFFSET] [-overwrite_original] <photos...>
```

### Options on the page

- **Camera timezone**: cameras store local time without a zone. Set the zone the camera
  clock was in (e.g. `+02:00`). If empty, exiftool assumes the server's local timezone.
- **Clock correction / geosync**: fixes a camera clock that was off, e.g. `+0:01:30`
  (see `exiftool -geosync`).
- **Overwrite originals**: when unticked, exiftool keeps `*_original` backups.

## Security note

There is no authentication. Only run it on a trusted network, or bind to a specific
interface with `--host`.
