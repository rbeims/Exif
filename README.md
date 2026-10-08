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

## Upload to Immich (optional)

Create an API key in Immich (**Account settings → API keys**) with at least the
`asset.upload`, `album.read`, `album.create` and `albumAsset.create` permissions (or "all"), then:

```sh
export IMMICH_API_KEY=xxxxxxxx
python3 geotag_server.py --immich-url http://localhost:2283
```

The page then shows an **Immich album** field, an **Upload to Immich** button and an
"upload to Immich after geotagging" checkbox (on by default). Photos are only uploaded
automatically when exiftool succeeded. Immich deduplicates by checksum, so pressing the button
again just reports the photos as "already there" and re-adds them to the album.

Upload geotagged photos only from cameras whose photos do *not* also reach Immich another way
(e.g. via the phone app): geotagging changes the file, so Immich would see two different files.

## Google Drive inbox (upload from anywhere)

Upload photos and GPX files from a phone/iPad into a Google Drive folder; the server copies
new files down every few minutes, geotags them and uploads them to Immich. Nothing is ever
deleted from Drive, so Drive doubles as a backup of the untouched originals.

### One-time rclone setup

1. Install rclone on the server (`sudo apt install rclone` or https://rclone.org/install/).
2. Run `rclone config` → `n` (new remote) → name `gdrive` → storage `drive` → leave client
   id/secret empty → scope `1` (full access; `drive.file` can't see files uploaded by the
   Drive app) → no advanced config → answer **n** to "Use web browser to automatically
   authenticate?" (the server has no browser).
3. rclone prints an `rclone authorize "drive" ...` command: run it on any computer with a
   browser and rclone installed, log in, and paste the token it prints back into the server.
4. Create the folder `GeotagInbox` in Drive and check: `rclone lsf gdrive:GeotagInbox`.

### Run

```sh
export IMMICH_API_KEY=xxxxxxxx
python3 geotag_server.py --immich-url http://localhost:2283 \
    --inbox-source gdrive:GeotagInbox --timezone +02:00 --album "Trips"
```

Every `--interval` seconds (default 300) it:

1. runs `rclone copy gdrive:GeotagInbox <dir>/inbox/mirror` (only new files are downloaded;
   the mirror is never modified, so it is a second copy of the originals);
2. copies each new photo to `<dir>/inbox/work`;
3. sends photos that already have GPS (e.g. phone pictures) and videos straight through;
4. geotags the rest with **every** GPX file in the inbox (`--timezone`, `--geosync` apply);
5. uploads geotagged photos to Immich (into `--album`, if given) and deletes the work copy.
   Without Immich they are moved to `<dir>/inbox/done`.

Photos that no track covers yet stay **waiting** and are retried whenever a GPX file is added,
so the track can be uploaded hours after the photos. `--upload-untagged-after HOURS` uploads
them without GPS after that long (default: wait forever). Progress is kept in
`<dir>/inbox/state.json`, so files are processed once even though they stay in Drive. The web
page shows the waiting photos and a log, and has a **Sync now** button.

Already have something else syncing a folder? Use `--inbox-dir /path/to/folder` instead of
`--inbox-source`.

### Run it as a service (systemd)

```ini
# /etc/systemd/system/geotag.service
[Unit]
Description=Photo geotagger
After=network-online.target

[Service]
User=youruser
WorkingDirectory=/srv/geotag
Environment=IMMICH_API_KEY=xxxxxxxx
ExecStart=/usr/bin/python3 /srv/geotag/geotag_server.py --dir /srv/geotag/uploads \
    --immich-url http://localhost:2283 --inbox-source gdrive:GeotagInbox --timezone +02:00
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

`sudo systemctl enable --now geotag`. Run `rclone config` as the same user as `User=`.

## Security note

There is no authentication. Only run it on a trusted network, or bind to a specific
interface with `--host`.
