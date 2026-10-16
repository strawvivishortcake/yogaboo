# Yogaboo -> Supabase -> web portal

```
 phone (Yogaboo app)            computer                         Supabase                    portal
 Download/YogabooApp/   --->   yogaboo_upload.py   --->   Postgres tables + `yogaboo`   --->  "Yogaboo" tab
 Participant_<id>/*.csv,wav     (parse + summarize)        private storage bucket              (charts)
```

| Folder | What it is |
|---|---|
| `uploader/` | `yogaboo_upload.py` reads the phone's files and pushes them to Supabase. `make_sample_data.py` writes fake recordings in the same formats for testing. |
| `portal/` | Your portal repo with a new **Yogaboo** tab added (new files under `src/components/yogaboo/`, `src/services/`, `src/pages/`; small edits to `App.jsx` and `DataSourceTabs.jsx`) and `sql/yogaboo_schema.sql`. |

## 1. One-time setup

1. **Database.** Paste `portal/sql/yogaboo_schema.sql` into the Supabase SQL editor and run it. It creates the
   tables, the private `yogaboo` bucket, and access rules (Researchers/Administrators read + write; Providers and
   Users read only participants linked to them; the public anon key gets nothing, since this is data from children).
2. **Portal.** Use the `portal/` folder as-is, or copy the new files + the two edited files into your repo. Then
   `npm install && npm run dev` as usual.
3. **Uploader.**
   ```
   cd uploader
   pip install -r requirements.txt
   cp .env.example .env      # then fill in the anon key and a Researcher/Administrator portal login
   ```

## 2. After EACH session: get the files off the phone and upload

> **Do this after every session.** Yogaboo overwrites a participant's files every time a test is run
> (it opens each file with append = off). If the same Participant ID is used for baseline, middle and end,
> the earlier session is gone from the phone as soon as the next one is recorded.

Files live in `Download/YogabooApp/Participant_<id>/` on the phone. Copy that folder to the computer by USB file
transfer, or with adb:

```
adb pull /sdcard/Download/YogabooApp ./YogabooApp
```

Then upload, labelling the session:

```
python yogaboo_upload.py ./YogabooApp/Participant_Subject1 --session baseline --age 11 --screen 1080x2173
python yogaboo_upload.py ./YogabooApp --session middle          # all participants in the folder
python yogaboo_upload.py ./YogabooApp --dry-run                 # parse and print only, no network
```

* `--session` is the label shown in the portal (baseline / middle / end, or anything). If omitted it is
  `session-YYYY-MM-DD` from the recording date. Re-uploading the same participant + label **replaces** that session.
* `--age` / `--portal-user-id` describe one participant, so they need a single `Participant_<id>` folder.
  `--portal-user-id <uuid>` links the participant to a portal account so that user (or their provider) can see it.
* `--screen WxH` (e.g. `1080x2173`, the size shown on the phone's gaze charts) lets the portal scale the gaze charts
  exactly like the phone; without it they are scaled to the data.
* The uploader signs in with the portal login in `.env` (a bare username becomes `username@fakewebsite.com`, the same as
  the portal's login page). Alternatively set `SUPABASE_SERVICE_ROLE_KEY` instead; that key bypasses all security,
  so keep it off shared machines.

## 3. View it

Open the portal -> **Yogaboo** tab -> pick a participant. You get a "change across sessions" chart (wobble index,
fixation spread, gaze path, balance sway, voice RMS: one point per session), then per session: accelerometer + gyroscope,
gaze tracking path, gaze fixation (centroid + spread ring), voice amplitude, and balance centre-of-pressure, plus
download buttons for the raw files.

## What was checked, and what was not

Checked: the file formats and the phone's own chart math were read out of the APK; the uploader's parsing was
tested on synthetic files and its full upload sequence against a local mock of the Supabase endpoints; the portal
builds, and each chart was rendered from sample data and inspected visually.
**Not checked: a real run against your Supabase project and a real phone recording.** Do a first upload with one
participant, then look in the Table editor / Storage and in the portal before relying on it.

Known approximations: the outlier filter reproduces the phone's 1.8-standard-deviation threshold per axis, but the
phone's exact rule could not be recovered, so outlier counts may differ slightly from the phone's. The phone's
"avg spread" figure is not reproduced; the portal reports mean distance to the centroid instead. The phone's
calibration values live inside the app's private storage and are not in the exported files, so they are not uploaded
(optional `--calibration-json` if you export them yourself). The recordings are identified only by the Participant ID
typed into the app: use codes, not names.
