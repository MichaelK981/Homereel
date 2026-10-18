# HomeReel

A minimal, self-built Plex/Jellyfin alternative: scans your Movies, Series,
and Anime folders, exposes a REST API, and streams video with proper seek
support. It remembers where every account got to in every title, so playback
picks up where it left off. No external metadata service and no telemetry —
just your files, indexed, access-controlled per user, and playable.

## Quick start

1. Point the volumes in `docker-compose.yml` at your Movies, Series and Anime
   folders, plus a cache folder for the index, artwork and transcode scratch.
2. `docker compose up -d --build`
3. Make the first account. There is no sign-up page, so it is created from the
   host:
   ```bash
   docker exec homereel python3 -c \
     "from app import auth; auth.create_user('YourName', 'a-good-password', 'admin')"
   ```
4. Open `http://<server-ip>:8000` and sign in. Change the password from
   **Settings → Your account**; further accounts are made in the UI.

This README is the documentation — how to set it up, and how it behaves once
it is running. `docs/guide.txt` covers the setup half on its own, in plain text,
for when you are standing at a box where the app will not start; it is generated
into a page by `docs/render.py` and is not read by the running app.

## Environment

Everything is optional except the paths; the defaults below are what the image
uses if you say nothing.

| Variable | Default | What it does |
|---|---|---|
| `MOVIES_PATH` / `SERIES_PATH` / `ANIME_PATH` | `/movies` `/series` `/anime` | Seed the three built-in libraries on first run. After that library paths live in the database, so changing these does not move an existing library. |
| `DB_PATH` | `/cache/library.db` | The SQLite index |
| `POSTER_DIR` | `/cache/posters` | Cached posters and episode banners |
| `TRANSCODE_DIR` | `/cache/transcode` | HLS scratch space |
| `SCAN_INTERVAL` | `300` | Seconds between background change scans; `0` turns the watcher off |
| `ENCODER` | `nvenc` | `nvenc` (h264_nvenc) or `cpu` (libx264) |
| `WATCHED_PERCENT` | `85` | How much of a title earns a check mark and takes it off Continue Watching |
| `SKIP_INTRO_SECONDS` | `85` | Where the "Skip Intro" button jumps to on an episode. There is no per-title intro detection, so this is one fixed offset for every Series/Anime episode; the button only shows while playback is still short of it. Movies never get the button. |
| `RESUME_MIN_SECONDS` | `30` | How far in playback must get before the position is kept |
| `RESUME_FINISHED_PERCENT` | `95` | Past this a title counts as finished and starts over next time |
| `WATCHING_TIMEOUT` | `90` | Seconds of silence before the Live view forgets a viewing |
| `SESSION_DAYS` | `30` | Session cookie lifetime |
| `PENDING_LOGIN_MINUTES` | `10` | How long a sign-in may sit between the password and the 6-digit code |
| `TRUST_PROXY` | unset | Set only when a reverse proxy in front sets `X-Forwarded-For`. Without it that header is ignored, because otherwise any client can choose the address the Live view shows. |
| `TZ` | `Etc/UTC` | Container timezone |
| `RADARR_URL` / `RADARR_API_KEY` | — | Movies: posters, ratings, collections |
| `SONARR_URL` / `SONARR_API_KEY` | — | Series: posters, ratings, episode banners and titles |
| `SONARR_ANIME_URL` / `SONARR_ANIME_API_KEY` | — | The same, from a second instance |
| `RADARR_ANIME_URL` / `RADARR_ANIME_API_KEY` | — | Posters, ratings and cast for films in any other mapped library (e.g. anime films), from a second Radarr |
| `RADARR_ANIME_PATH_MAP` | — | `<radarr path>:<homereel path>`, e.g. `/movies:/animemovies`. Only needed when that Radarr mounts the folder at a different path than HomeReel does — artwork is matched by path, so the two must agree after mapping |
| `DOWNLOADS_URL` | — | Address of a page hosting the Android/TV app builds. When set, Settings shows a "Get the apps" panel and the sign-in card a link to it; unset, neither appears. Must start with `http://` or `https://` |
| `TMDB_API_KEY` | — | Cast for the description/cast popup. Movies get theirs from Radarr's own credit endpoint regardless; Sonarr has no equivalent, so Series/Anime cast needs this — a free key from themoviedb.org. Without it, shows still get a description, just no cast row. |
| `NVIDIA_VISIBLE_DEVICES` / `NVIDIA_DRIVER_CAPABILITIES` | — | GPU passthrough. The capabilities list **must** include `video`, or NVENC is silently absent and transcodes fall back to CPU. Do not use `all` inside an LXC. |

## Expected folder layout

Movies want a `(Year)` in the filename; Series and Anime both use the same
Show/Season/Episode convention (Anime is just a separate library from
Series, not a different layout):

```
Movies/
  Inception (2010)/
    Inception (2010).mkv

Series/
  Breaking Bad/
    Season 01/
      Breaking Bad S01E01.mkv

Anime/
  Attack on Titan/
    Season 01/
      Attack on Titan S01E01.mkv
```

The scanner just needs a `(Year)` somewhere in movie filenames and an
`SxxEyy` pattern somewhere in episode filenames — it doesn't care about
exact naming beyond that. Series and Anime are tracked as separate
libraries internally, so a show with the same name in both won't collide.

## REST API

| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/library` | Counts of movies, and shows/episodes per library |
| GET | `/api/movies` | List all movies |
| GET | `/api/shows?library=series\|anime` | List show names in that library |
| GET | `/api/shows/{show}/episodes?library=series\|anime` | Episodes for a show |
| GET | `/api/item/{id}` | Raw metadata for one item |
| GET | `/api/search?q=` | Search titles across the libraries you can see |
| GET | `/api/home` | Continue Watching, plus the Home shelves |
| GET | `/api/continue` | Continue Watching on its own |
| GET | `/api/plays` | Your recent play history |
| GET | `/api/resume/{id}` | Where you left off in one title, in seconds |
| GET | `/api/watched` | What you have watched: item ids, finished shows, finished collections |
| POST | `/api/progress` | Report playback position — `{item_id, position, duration}` |
| GET | `/api/watching` | Who is playing something right now (admin) |
| GET | `/api/collections` | Collections with 2+ films on disk |
| GET | `/api/collection?name=` | One collection's films |
| GET | `/api/poster/item/{id}` | Poster for an item (an episode answers with its show's) |
| GET | `/api/poster/show/{library}/{show}` | A show's poster |
| GET | `/api/still/item/{id}` | One episode's banner (404 when it has none) |
| GET | `/api/artwork/status` | Artwork counts by source and library |
| POST | `/api/artwork/sync` | Re-sync artwork, `?force=true` re-downloads (admin) |
| GET | `/api/browse?path=/` | Folder picker for mapping libraries (admin) |
| GET | `/api/libraries` | Mapped libraries with their paths (admin) |
| POST | `/api/libraries` | Map a new library (admin) |
| DELETE | `/api/libraries/{name}` | Unmap a library; files untouched (admin) |
| POST | `/api/scan` | Full re-scan: re-parse and rewrite every file found (admin) |
| POST | `/api/scan/changes` | Partial scan: index only what changed (admin) |
| GET | `/stream/{id}` | Streams the video file (Range-request aware) |
| GET | `/transcode/{id}/{quality}/{start}/playlist.m3u8` | HLS playlist, transcoded from `start` seconds in |
| POST | `/api/auth/verify` | Second factor for an enrolled account — `{code}` |
| GET | `/api/auth/setup` | Enrolment QR + secret, for a first sign-in |
| POST | `/api/auth/setup` | Confirm enrolment — `{code}`; returns the backup codes |
| POST | `/api/auth/2fa/new` | Start moving to a new authenticator — `{password}` |
| POST | `/api/auth/2fa/confirm` | Finish the move — `{code}`; returns new backup codes |
| POST | `/api/auth/2fa/backup-codes` | Reissue backup codes — `{password}` |
| GET | `/api/auth/me` | Who you are signed in as, and which libraries you have |
| POST | `/api/auth/quickconnect/start` | Begin a Quick Connect pairing — returns `{token, code, expires_in}` |
| GET | `/api/auth/quickconnect/poll/{token}` | Poll a pairing — `{status: pending\|approved\|expired}`, plus `session_token`/`username` once approved |
| POST | `/api/auth/quickconnect/approve` | Approve a pairing from an already-signed-in client — `{token}` |
| GET | `/api/users` | List accounts, with their library access (admin) |
| POST | `/api/users` | Create an account — `{username, password, role, libraries}` (admin) |
| PUT | `/api/users/{name}/libraries` | Replace one account's library access (admin) |
| DELETE | `/api/users/{name}/2fa` | Un-enrol an account whose phone is gone (admin) |
| DELETE | `/api/users/{name}` | Delete an account and revoke its sessions (admin) |

Every endpoint except the login page and its POST needs a session cookie —
`/stream` and `/transcode` included, so media is never reachable unauthenticated.
Anything scoped to a library answers **403** when that library is not yours.

Interactive docs are auto-generated by FastAPI at `/docs`.

Both scans take an optional `?library=<name>` to limit them to one library.

## Scanning

Two scans, because they cost very different things:

- **Full** (`POST /api/scan`, *Rescan Library*) walks every library and rewrites
  every row whether or not the file changed. Reach for it after renaming files
  or changing how titles are parsed — it is the one that re-reads what a file
  *means*.
- **Partial** (`POST /api/scan/changes`, *Check for Changes*) walks the same
  folders but compares each file's path, mtime and size against the index and
  writes only the difference: new files are added, replaced files updated, and
  rows whose file is gone are deleted. A pass that finds nothing writes nothing,
  so it is cheap enough to run on a timer.

The partial scan runs by itself every `SCAN_INTERVAL` seconds (default 300, set
it to `0` to switch the watcher off), and once at startup — a container restart
only has to pick up what changed while it was down, rather than re-walking the
whole pool. A first run against an empty index still does the full scan, since
there is nothing to diff against. When a partial scan finds new files it kicks
off an artwork sync for them; nothing else asks Radarr/Sonarr.

It is a folder walk on a timer, not a filesystem watch: inotify does not cross
the bind mount into the container, and the *arr apps import files behind the
app's back. So new media appears within `SCAN_INTERVAL` of landing, not
instantly — press *Check for Changes* if you do not want to wait.

**A library is never emptied by a partial scan.** If its path is not mounted, or
if a single pass would take it from full to zero files, it is reported back with
a status of `unavailable`/`empty` and left exactly as it was — an unmounted
volume is indistinguishable from a mass deletion, and the wrong guess throws
away the index. Actually emptying a library is what the full rescan is for.

Both scans hold a lock, so the watcher and a button press queue rather than
colliding: they each write through one connection inside one transaction, and
two at once would block for 30 seconds and then raise *database is locked*.

## Using it

**Sidebar** — Home, then one entry per library you can see, then **Live**
(admins only), then Settings. The collapse button is under the wordmark;
collapsed, the sidebar is icons only. Movies is a disclosure group with
Collections under it.

**Home** — **Continue Watching** first, then "recently added" shelves, one per
library, up to 24 tiles each.
Recency is the file's own mtime, not when it was indexed, so a first scan does
not dump the whole library into the shelf on one timestamp. Show libraries shelve
the *show* with an "N episodes" caption rather than every episode.

**Movies** — a poster wall; titles appear on hover, the year is the caption.
Anything you have watched wears a check mark and its poster recedes until you
point at it.

**Collections** — TMDB collections as Radarr reports them, listed once more than
one film of the set is on disk, and opened in release order.

**Series / Anime** — a poster wall of shows; click one for its episodes.

**Episode grid** — a 16:9 banner per episode, ordered by season then episode,
labelled `S01E04` with the episode title under it. An episode with no banner
falls back to a title tile rather than borrowing the show's portrait poster.

**Search** — the header box searches titles and show names as you type, across
every library you can see.

**Rescanning** — two buttons in the header (admin): *Check for Changes* for the
cheap pass, *Rescan Library* for the full one. See **Scanning** below.

**Player** — the browser's own controls, with **Direct Play** streaming the
file untouched and **1080p / 720p / 480p** starting an on-demand transcode.
Playback picks up where you left off, and says so with a **Start from the
beginning** next to it. Switching quality mid-film keeps your place.

**Settings** — your own password (8+ characters, needs the current one); for
admins, the library map and the user table.

**Mapping a new library** (admin) — bind-mount it in `docker-compose.yml` and
restart first: HomeReel only sees what the compose mounts, so a folder that is
plainly there on the host is invisible until it is. Then Settings → Libraries,
browse to it in the picker, name it, pick Movies-style or Shows-style (guessed by
looking for an `SxxEyy` a couple of levels down), and map it — it is indexed
immediately. Unmapping deletes the library's index rows, artwork and grants and
**never touches the files**; the three built-ins cannot be unmapped.

## Artwork

How a **poster** resolves, per title:

1. A local image in the title's own folder wins: `poster.jpg`, `poster.png`,
   `poster.jpeg`, `folder.jpg`, `cover.jpg`.
2. Otherwise the title is matched in Radarr/Sonarr **by folder path** and the
   poster comes from that service's own cache.
3. Otherwise nothing, and the card is a plain title tile.

How an **episode banner** resolves, per episode file:

1. A local image named after the video: `MyShow.S01E01.jpg` / `.png` / `.jpeg` /
   `-thumb.jpg` / `-thumb.png`.
2. Otherwise Sonarr's screenshot for that episode, matched on the imported file
   path first and season/episode numbering second.
3. Otherwise nothing, and the grid shows a title tile.

Unlike posters, Sonarr keeps **no local copy** of episode screenshots — the API
only reports a remote URL on TVDB's CDN — so banners need outbound internet.
Downloads run 8 at a time and are cached in `POSTER_DIR` for good.

`POST /api/artwork/sync` re-matches everything and fetches what is missing;
`?force=true` re-downloads what is already cached; `GET /api/artwork/status`
counts what is held. A sync also prunes artwork for episodes that have left the
index and deletes those cached images — never your own local overrides. In its
stats, `missing` means the title or episode exists but has no image upstream,
while `shows_unmatched` means the show's folder is not a series in either Sonarr
— which is the usual reason a show has no art at all.

A banner is keyed by the episode's **file path**, not its numbering: a show can
be re-numbered or re-imported, and a path that no longer exists is a row that can
safely be pruned.

## Description and cast

Clicking any movie, show, or episode opens a popup with its description and
cast before playing — a `View episodes` or `Play` button underneath is what
actually acts on the selection. It rides the same artwork sync as posters
(`POST /api/artwork/sync`), so it fills in on the same schedule and needs no
extra setup beyond artwork already working.

The description is the *arr's own `overview` field, already fetched for every
matched title. Cast is a separate lookup: **movies get it from Radarr's own
credit endpoint**, which Sonarr has no equivalent of, so **Series and Anime
cast comes straight from TMDB** instead, matched on the `tmdbId` Sonarr already
reports for the series — see `TMDB_API_KEY` above. An episode has no cast of
its own; its popup shows the episode's description with its show's cast.

## Troubleshooting

Things that go wrong, and what they usually mean.

**A show has no artwork while everything else does** — it is not managed by
Radarr/Sonarr at that exact path. Add it there, or drop a `poster.jpg` in its
folder.

**Some episodes have no banner** — TVDB genuinely has no screencap for them, or
the show is not matched in Sonarr at that path. Drop an image named after the
video beside it; it is picked up on the next sync.

**A library suddenly shows zero items** — it should not: a partial scan refuses
to empty a library. Check the mount; the scan result reports `unavailable` for a
path it cannot see. Only a full rescan can empty one.

**New media has not appeared** — the watcher runs every `SCAN_INTERVAL`
seconds, so give it that long or press *Check for Changes*.

**A rename did not take** — a partial scan reads it as one removal plus one
addition, which is right for the index; a file edited in place at the same size
and mtime is invisible to it. Full rescan covers both.

**A UI change did not appear** — `app.js`, `style.css` and the vendored
`hls.min.js` are loaded with a `?v=<content hash>` on their URL, rewritten
into `index.html` fresh at every container start, specifically so a stale
copy sitting in a browser, a PWA, or a CDN in front of HomeReel can't keep
serving itself forever: a changed file gets a URL nobody has fetched before,
so there is nothing to have cached wrong. If a shipped change still isn't
showing, confirm the *container* actually restarted on the new image
(`docker ps`) — a `docker compose up -d` that finds no image/config change
plays it safe by leaving the existing container alone rather than restarting
it, so a rebuild without a version bump doesn't reliably deploy.

**The cache keeps growing** — `/cache` holds the index, cached artwork (roughly
60–90 KB an image) and transcode scratch, which is cleaned up five minutes after
a session goes idle.

**Locked out after failed logins** — five failures per username+IP lock that pair
for 60 seconds.

**Forgotten admin password** —
`docker exec homereel python3 -c "from app import auth; auth.set_password('YourName', 'new-password')"`

**Quality switching fails while Direct Play works fine** — hls.js is vendored
at `static/vendor/hls.min.js` rather than pulled from a CDN, specifically so a
client's DNS has nothing to do with whether transcoded playback works. If this
regresses (a `<script src="https://...">` creeps back in), any viewer whose DNS
can't resolve that host loses every quality but Direct Play, with the browser
reporting `NotSupportedError: no supported sources` on the video element.

## Live: who is watching

An admin-only **Live** entry in the sidebar lists everyone playing something
right now — what it is, how it is being delivered, roughly how far in they are,
which machine they are on, and how long they have had it open. It refreshes
every five seconds while the tab is open, and stops when it is not.

Nothing reports in for this. A viewer is by definition pulling media off the
server continuously — byte ranges in direct play, one HLS segment every six
seconds in transcode — so the requests *are* the signal. That makes it truthful
about any client, including one hitting `/stream` with curl, and it needs no
cooperation from the player.

Position is inferred from what was asked for, which is why the view shows it as
approximate:

- **Direct Play** gives a byte offset. That is an exact fraction of the *file*
  and only roughly a fraction of the running time — a browser reads ahead of the
  picture, and a variable bitrate does not map evenly onto seconds.
- **A transcode** gives a segment number, which is exact: segment N is six
  seconds in per N.

Runtimes come from one `ffprobe` per title, run in the background and cached, so
a percentage may take a moment to appear on something never played before. Until
it does, the row says *position unknown* rather than guessing.

A viewing drops off the list after **90 seconds** without a request — long
enough that pausing does not look like leaving. Set `WATCHING_TIMEOUT` to change
it. The whole registry lives in memory: it is written to several times a minute
per viewer, and "currently watching" is a fact about right now that should not
survive a restart. The durable history is still the `plays` table.

Transcodes are badged in the accent colour and Direct Play is not, because a
transcode is an ffmpeg session on the GPU and that is the thing worth spotting
across a list.

## Resume: picking up where you left off

Every player reports its position back every 15 seconds, on pause, and on close.
Opening a title again asks the server where that was and seeks there — so a film
started on a laptop carries on from the same place on a phone. A **Resuming
from 42:15** bar appears in the player with a **Start from the beginning** next
to it, so it is visible and undoable rather than a surprise.

What is kept is the **furthest point** of the viewing, not the last frame shown:
rewinding to catch a line of dialogue does not cost you the place you had
reached. Switching quality mid-film keeps it too.

Two things start from the beginning instead:

- **A title played past `RESUME_FINISHED_PERCENT`** (default 95). It was
  finished, and dropping you into the closing credits is not a resume. The bar
  sits high on purpose: stopping at 90% usually means you intend to come back
  for the last ten minutes.
- **A viewing under `RESUME_MIN_SECONDS`** (default 30). Below that the title
  was opened, not started.

The runtime a position is measured against is the **server's**, ffprobed from
the file on first playback and cached on the item. In transcode mode the browser
only knows the length of the HLS playlist ffmpeg has written so far, so trusting
it would call a film finished ten minutes in. If a file has no readable duration
at all, the player's number is used instead.

**Direct Play** seeks the file itself. A **transcode** cannot — its playlist only
holds what ffmpeg has produced — so resuming starts a *new* ffmpeg at that offset
(`-ss`), and the offset rides in the stream's URL:
`/transcode/{id}/{quality}/{start}/playlist.m3u8`. It is in the path rather than
a query string because ffmpeg names its segments relatively, so the browser
resolves them against the playlist's own URL and lands in the right session. The
player's clock therefore counts from the resume point in that mode, and the
offset is added back on before any position is reported — including to the Live
view, which would otherwise show a resumed stream as being at the start.

Positions live in the `plays` table alongside the history. Note that a play row
is written the moment a stream is requested, before the player has said
anything, so a row still sitting at zero is a placeholder and is ignored when
working out where to resume — otherwise merely reopening a film would erase the
place you opened it to return to.

## Next/Previous episode

Playing an episode of a Series or Anime title shows a **Previous Episode** /
**Next Episode** bar above the video. Movies never get one — the bar stays
hidden for anything that is not an episode.

The pair is derived fresh every time a title starts playing, not carried over
from wherever the episode grid last was: a title reached from **Continue
Watching** or **Search** gets the same bar as one reached by opening its show,
because the player asks `/api/item/{id}` for the episode's show and library and
then re-fetches that show's episode list to find its neighbours. The buttons
disable themselves at the first and last episode of the show rather than
wrapping around or crossing into another season.

Clicking either one is exactly like clicking the neighbouring episode's own
card: it goes through the normal resume lookup, so a next episode already
partly watched picks up where it left off instead of restarting.

## Watched marks

A title played to at least **`WATCHED_PERCENT`** of its runtime (default 85)
earns a check mark on its card, and its poster dims until you hover it. Starting
something and abandoning it puts it in the history but leaves the card unmarked.

Marks come back from one call, `/api/watched`, in the three identities cards are
keyed by:

- **item ids** — films and individual episodes
- **[library, show] pairs** — a show gets a mark only when *every* episode is
  watched. A check on a show you are three episodes into would be a lie, and
  because shows are counted from the index, a newly grabbed episode silently
  un-marks its show.
- **collection names** — same rule, for a collection whose every film is watched

One call decorates every grid, which is also what lets a mark appear right after
playback without refetching anything.

Note the two bars are deliberately different numbers. **85%** earns the check
mark and takes a title off Continue Watching; **95%**
(`RESUME_FINISHED_PERCENT`) is where resume stops offering a position and starts
over. Between them a title is marked watched, is off the shelf, and would still
resume near its end if you deliberately played it again.

## Continue Watching

The first shelf on Home is where you left off, newest first, with a progress bar
under each tile. Two kinds of entry land on it:

- **A film you stopped part-way through**, captioned with how much is left.
- **A show's next episode.** A series takes one place on the shelf, not one per
  episode: the episode you are in the middle of if there is one, otherwise the
  first unwatched episode after the last one you finished, captioned *Next up*.
  A show with nothing left drops off entirely.

Clicking a tile plays straight into its saved position — the shelf already knows
it, so the player does not have to ask again.

Episodes appear under their **show's** poster rather than their own banner: the
shelf is portrait tiles like every other one on Home, and what you are coming
back to is the series, not the frame the banner was cut from.

## Authentication

Username and password, with **server-side sessions** — not a signed cookie or a
JWT. That choice is what makes revocation immediate: signing out, deleting an
account, changing a password or withdrawing a library all take effect on the
very next request, rather than waiting out a token's expiry.

**Passwords** are stored as PBKDF2-HMAC-SHA256 at 600,000 iterations with a
per-user 16-byte salt, in a self-describing `pbkdf2_sha256$<iters>$<salt>$<key>`
field — the iteration count travels with each hash, so raising it later leaves
old hashes verifiable. Comparison is `secrets.compare_digest`. A login for an
account that does not exist still runs a full hash against a dummy, so a missing
user and a wrong password take the same time to answer and accounts cannot be
enumerated with a stopwatch.

**Sessions** hand the browser an opaque `secrets.token_urlsafe(32)` and nothing
else — no username, no role, no claims. The cookie is `HttpOnly` (an XSS cannot
read it) and `SameSite=Lax` (which is what makes CSRF a non-issue here), and
lasts `SESSION_DAYS`. Every request re-reads the session row, and library grants
are read fresh alongside it rather than baked in at login.

**Only a SHA-256 of each token is stored.** The database therefore holds nothing
that can be replayed as a cookie: reading `library.db` reveals neither passwords
nor usable sessions. Plain SHA-256 rather than PBKDF2 is deliberate — the input
is 256 bits of `secrets` output, not a guessable human password, so there is
nothing to iterate through and no reason to make every request pay for
stretching. Databases written before this are converted in place on first start,
which uses the raw token that is already there, so the upgrade signs nobody out.

Failed logins are throttled 5 per username+IP per 60 seconds, and expired
sessions are swept on each successful login. Wrong-password probes count too
wherever the password is re-checked — changing it, or setting up a new
authenticator — so a stolen session cannot be used to guess it unthrottled.

## Two-factor authentication

**Every account has to enrol. There is no opt-out and no per-user toggle.** The
second factor is a standard TOTP code (RFC 6238: HMAC-SHA1, 6 digits, 30-second
step), so any authenticator works — Google Authenticator, Aegis, Raivo, 1Password,
Bitwarden, a hardware token that speaks TOTP.

**Signing in is two steps, and so is the state behind it.** A correct password
buys nothing but a short-lived row in `pending_logins` and a `mms_pending`
cookie; only a valid code turns that into a real session. The pending cookie is
not a session and is refused by everything else, so there is no window in which
a password alone can read media. It lasts `PENDING_LOGIN_MINUTES` (10 by
default) and is thrown away as soon as it is spent.

**First sign-in is the enrolment screen.** A user with no authenticator yet gets
a QR (and the base32 key, for typing it in by hand) instead of the code prompt,
confirms one code to prove the app has the secret, and is signed in — there is
no way past the screen and no way to reach the library without going through it.

**Ten backup codes** are issued at enrolment and shown exactly once. Each works
once, in the same box the 6-digit code goes in, and case, spaces and dashes are
ignored when typing one back. Only a SHA-256 of each is stored — the same
reasoning as session tokens: 12 characters from a 31-symbol alphabet is ~59 bits
of entropy generated here, not a guessable human secret. Confirming a new
authenticator always reissues the whole set; Settings → Two-factor can also
reissue them on demand, which voids the old ones.

**Codes cannot be replayed.** The counter each account last spent is stored, and
anything at or below it is refused, so a code read over a shoulder is dead for
the rest of the 30 seconds it would otherwise still be live. One step of drift
either side is accepted, which covers a phone clock up to half a minute out.

**Changing phone** is Settings → Two-factor → *Set up a new authenticator*. It
asks for the password again — a session alone is not enough to hand the second
factor to a different device — and the new secret is held separately until a
code from it is confirmed, so the old authenticator keeps working right up to
the swap and an abandoned setup strands nobody.

**Lost phone.** An admin can un-enrol anyone from Settings → Users → *Reset*.
That signs that account out everywhere, voids their backup codes, and puts them
back on the enrolment screen at their next sign-in, using the password they
already have. It works on your own row too.

**If the only admin is locked out** — phone gone, backup codes gone — reset it
from the host, which is the one thing no amount of cookie or code can gate:

```bash
docker exec -w /srv homereel python -c \
  "from app import auth; auth.reset_two_factor('YourName')"
```

They then enrol again at the next sign-in. The same call clears anyone.

**Upgrading an existing install signs everyone out once.** The migration that
adds the columns also empties `sessions`: every cookie in there was issued
against a password alone, and leaving them valid would mean 2FA did not actually
start applying to anybody until they aged out a month later. Everyone signs in
again, and enrols as they do.

## Quick Connect

Sign in a keyboard-less device — the TV app, currently the only client that
uses it — by scanning a QR code with a phone that is already signed in,
instead of typing a username, password and 2FA code with a remote.

1. The device with no keyboard calls `POST /api/auth/quickconnect/start`
   (no session needed) and gets back a `token` (goes in a QR it draws itself),
   a short `code` shown beside the QR as a plain-language sanity check, and
   `expires_in` seconds (`QUICK_CONNECT_MINUTES`, 5 by default).
2. It polls `GET /api/auth/quickconnect/poll/{token}` on a timer while the QR
   is on screen.
3. An already-signed-in phone scans the QR and calls
   `POST /api/auth/quickconnect/approve` with `{token}`, riding on its own
   real session — this is why it is not in the public path list the way the
   other two are: the phone already proved who it is, and that is who the new
   session belongs to.
4. The next poll answers `{status: "approved", session_token, username}`. The
   device stores `session_token` as its own `mms_session` cookie value
   directly — there is no `Set-Cookie` here, since the response reaching an
   unauthenticated poll is a JSON body, not a browser turning a login form
   into a session the normal way.

The pairing is single-use: the moment a poll picks up an approved token, the
row is deleted, so a retried or duplicated poll cannot see it twice. It is
also single-shot in the other direction — approving a token twice, or an
expired one, fails. No profile carries over from the phone; the device lands
on the profile picker the same as any other fresh sign-in, since picking a
tile is quick even without a keyboard.

## Users and library access

Accounts have a role (`admin` or `viewer`) and a set of libraries. **The set is
chosen when the account is created** — Settings → Users → *Add a user* has a
Movies/Series/Anime tick list — and can be changed later from the same table by
ticking or unticking a library on that user's row, which saves immediately.

- A library a user does not have is hidden from their sidebar *and* refused by
  the API, including `/stream`, `/transcode`, posters and search. Hiding the tab
  is only cosmetic; the server is what enforces it.
- Grants are read fresh on every request, so revoking a library takes effect on
  that user's very next click rather than at their next sign-in.
- **Admins always have every library.** They can grant themselves anything
  through the user API anyway, so the picker is ticked and locked for them.
- Accounts that existed before this feature keep access to everything: the
  first run after the upgrade backfills the grant table.
- A new account is created with a password only. **The person enrols their own
  authenticator at their first sign-in** — an admin never sees or holds their
  second factor, only the ability to reset it.

## Profiles

Signing in gets an account to a **profile picker**, sitting between login and
the app the same way Netflix splits an account from who's actually watching.
Profiles are new on top of the real account+password+2FA sign-in, not a
replacement for it — the account still owns the credentials and the 2FA
enrolment; a profile just says which person's watching right now.

- **Up to 5 profiles per account.** Add/rename/delete from the picker's
  *Manage profiles* mode; an account always keeps at least one, so the last
  profile can't be deleted.
- **My List, Continue Watching, watched marks and resume position are all
  per-profile**, not per-account — switching profiles switches all four.
  Admin access, library grants and 2FA stay account-level; there's no
  per-profile permission split.
- Accounts that existed before this feature got a default profile named
  after their username on first run, with their existing list and watch
  history carried over onto it — nothing was lost in the upgrade.

## Appearance

Settings → Appearance has admin-only knobs that change how the whole
app is laid out, for every account:

- **Navigation** — `rail` (the sidebar) or `top` (a header bar).
- **Poster style** — `portrait` (2:3 movie-poster cards) or `still`
  (16:9 cards, used for still-driven catalogs like screenshots).

Both are one shared setting for the whole install, not a per-user
preference — read by anyone signed in, changed only by an admin.

The same panel also sets the **household theme** — see Themes below.

## Themes

Settings → Theme offers 21 visual themes, picked **per profile**: each
profile in an account can run a different one, and a profile that hasn't
picked follows the household theme an admin sets under Appearance (which is
also what the sign-in page uses on a browser that has never signed in).
Picking a tile switches immediately; there is no Save.

| Theme | Look |
|---|---|
| HomeReel | The house style: frosted glass over a teal glow (default) |
| Particles | Cinematic dark, warm key light, drifting motes of light |
| Liquid Morph | Slow morphing colour blobs, pebble-round surfaces, gradient pills |
| Neon Glow | Black glass, glowing cyan/magenta tube borders and type |
| Y2K Chrome | Brushed-metal gradients, glossy chrome pills, chrome lettering |
| Kinetic Type | Huge headlines that rise in word by word, hollow alternating words |
| Paper Cutout | Kraft paper, tilted photo prints, masking tape, torn hero edge |
| Holographic | Iridescent foil on posters that follows the pointer |
| Isometric | Extruded tiles with stacked offset shadows on a 30° grid |
| ASCII Art | Phosphor terminal, monospace only, box-drawing headings, cursor |
| Clay 3D | Soft lavender claymorphism, puffy squeezable buttons |
| Retro VHS | Scanlines, colour bleed, tracking band, an on-screen PLAY/clock |
| Comic Book | Halftone newsprint, ink outlines, caption boxes, speech bubbles |
| Split-Flap | Departures-board tiles; headings flip in letter by letter |
| Hand-Drawn | Notebook paper, wobbly pencil boxes, highlighter, doodles |
| Blueprint | Drafting grid, white linework, figure labels and section marks |
| Physics | Spring easing, pointer tilt, squash on press, bounce-in content |
| Pixel Art | 8-bit pixel frames, console palette, stepped motion |
| Bauhaus | Primary colours, circle/square/triangle, lowercase Jost |
| Art Deco | Gold leaf on black, stepped frames, centred symmetric hero |
| Neo-Brutalism | Thick black borders, hard offset shadows, loud fills |

Motion (particles, blobs, flips, the VHS overlay…) never runs when the
device asks for reduced motion, and can be switched off per device with the
**Motion effects** checkbox. Every theme's fonts are self-hosted under
`static/fonts/themes/` — still no external requests — and a browser only
downloads the ones the active theme uses.

**Adding or changing a theme:** the registry is `static/themes.js` (one
entry per theme — id, name, tagline, swatch, light/dark, effect); the look
is a block in `static/themes.css` under `[data-theme="<id>"]` (tokens) and
`html[data-theme="<id>"] …` (component rules). The server reads the ids
straight out of `themes.js` to validate choices, so there is no second list
to update. A profile's choice is stored in `profiles.theme` (NULL = follow
the household); the household one is the `theme` setting.

## Install it on a phone

HomeReel ships a web app manifest and Apple's home-screen meta tags, so it
installs to a phone as a standalone app — own icon, own name, no browser
chrome, no expiry, nothing to sign.

- **iPhone/iPad**: Safari → Share → *Add to Home Screen*. Must be Safari; Chrome
  on iOS cannot install web apps.
- **Android**: Chrome → menu → *Install app*.

`manifest.webmanifest` and `/icons/` are the only static paths served without a
session, because a browser fetches a manifest without credentials and because
installing from the sign-in page has to work before anyone has signed in.

**There is no service worker, deliberately.** It would buy offline caching this
app has no use for — the media is all on the server — while reintroducing
exactly the stale-asset problem `RevalidatingStaticFiles` exists to prevent.
The cost is that Chrome will not show its install *prompt*; the menu item still
works, and iOS never needed one.

The player carries `playsinline`, without which an iPhone drags every title into
fullscreen the moment it starts — which, inside a standalone app, looks like
being thrown out of it.

## Transcoding

Each item's player has a **Direct Play / 1080p / 720p / 480p** switch. Direct
Play just streams the source file as-is (fast, no CPU/GPU cost). The quality
buttons spin up an on-demand ffmpeg process per session that transcodes and
serves it as HLS (`.m3u8` + `.ts` segments) — the same approach Jellyfin/Plex
use for clients that can't handle the source codec.

**GPU encoding (NVIDIA NVENC)** is the default (`ENCODER=nvenc` in
`docker-compose.yml`). Set it to `ENCODER=cpu` to fall back to libx264 if you
ever need to run without the GPU.

**Everything is encoded to 8-bit H.264 (`format=yuv420p`), deliberately.** Most
anime releases — and a growing number of remuxes — are HEVC **Main 10**, and a
scale filter preserves the source's pixel format. Left alone, those 10-bit
frames reach `h264_nvenc`, which cannot encode 10-bit and refuses to open with
**`No capable devices found`** — an error that reads like a missing GPU and is
nothing of the sort. The CPU path has the same problem from the other side:
libx264 *will* accept 10-bit and quietly emit **High 10**, which browsers then
cannot decode. Forcing 8-bit fixes both.

**Direct Play failing is invisible to the server**, because the file was served
perfectly well — the browser simply would not decode it. Safari plays no
Matroska at all, so on an iPhone every `.mkv` fails this way. The player
therefore listens for the media error itself and switches to 1080p once per
title; picking Direct Play by hand from the quality row is treated as a decision
and left alone.

### One-time host setup for GPU transcoding

Your host needs the **NVIDIA Container Toolkit** so Docker can pass the GPU
into the container — the image itself doesn't need CUDA installed, just the
driver's `libnvidia-encode.so` at runtime.

```bash
# Ubuntu/Debian host, one-time setup:
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
  sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
  sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

Then `docker compose up -d --build` — the `deploy.resources.reservations`
block in `docker-compose.yml` requests the GPU (works with plain Compose,
no swarm mode needed).

**On a GTX 1050 Ti specifically:** it's Pascal-generation NVENC, so it
handles H.264 encode well (no HEVC encode support on this card, and NVENC on
consumer Pascal cards is limited by the driver to a small number of
concurrent sessions — typically 2-3 — so simultaneous transcodes beyond that
will queue/fail rather than degrade gracefully. Fine for one or two people
watching at once; if you regularly need more concurrent transcodes, that's
a hardware ceiling, not a config one.)

If a transcode fails to start, the API returns the tail of ffmpeg's actual
log in the error response (e.g. `Cannot load libcuda.so.1` if the container
toolkit isn't wired up correctly) — that's the fastest way to debug it.

- Transcode sessions are per `(item, quality)` and are reused if you're
  already watching that combination.
- Idle sessions (no segment requests for 5 minutes) are auto-killed and their
  temp files deleted by a background cleanup thread.
- **Known limitation**: segments are generated in real time roughly as fast
  as ffmpeg can encode. Seeking backward works (all segments are kept), but
  seeking far ahead of what's been encoded so far will stall until ffmpeg
  catches up — there's no seek-triggered re-encode-from-offset yet, which is
  what Jellyfin/Plex do for that case.

## Known limitations (MVP)

- **Poster art and ratings come from Radarr/Sonarr**, matched by path — a
  title neither of them manages gets no artwork unless you drop a `poster.jpg`
  in its folder.
- **Episode banners come from Sonarr's screenshots**, which are TVDB's — an
  episode TVDB has no screencap for shows as a plain title tile. Drop an image
  named after the video (`Show.S01E01.jpg`, or `Show.S01E01-thumb.jpg`) beside
  it to override. Unlike posters, Sonarr does not keep a local copy of these, so
  the first sync of a large library pulls a few thousand images off TVDB's CDN.
- **Session cookies are not `Secure`** — this is a plain-HTTP LAN app. Put it
  behind TLS before exposing it anywhere else.
- **The login throttle is in-process** — 5 failures per username+IP for 60
  seconds, but the counter resets when the container restarts and would not be
  shared between uvicorn workers if you ever ran more than one.
- **Changing a password does not sign that user's other sessions out.** Deleting
  the account does.
- **No subtitle handling yet.**
- **A partial scan won't notice a rename.** A renamed file reads as one removal
  plus one addition, which is correct for the index but loses the old row; a file
  whose *content* was edited in place without changing its size or mtime is
  invisible to it. Full rescan covers both.
- **The Live view's position is inferred, not reported** — see the section
  above. It is right to within a segment on a transcode, and approximate on
  direct play.

## Customizing the UI

The whole front end is three files in `static/`: `index.html`, `style.css`,
`app.js` — plain HTML/CSS/vanilla JS, no build step. Restyle freely; the
API underneath doesn't change.

## Version history

The source lives on GitHub (`MichaelK981/Homereel`): every release is a
commit on `main` tagged `vX.YY`. Built images are published to Docker Hub
(`michaelk98/homereel`) under the same version, with `latest` pointing at
the newest, and the live stack pulls from there rather than a local build.
Versions before 1.90 predate the repository and exist only as Docker Hub
tags; the ones listed below are summarised here.

- **1.77 (2026-08-28)** — Fixed a deploy regression: the previous build had
  been made from a stray, stale copy of the source tree and silently reverted
  GPU-decode (NVDEC) transcoding, the fix that stops multiple transcode
  sessions for the same title competing for the one GPU encoder, and
  per-track audio selection. This restores all three.
- **1.78 (2026-08-28)** — Restored real URL-based navigation: switching tabs
  now updates the address bar and Back/Forward moves between them, both lost
  in the same regression as 1.77. Added a server-side fallback so a hard
  refresh or a bookmarked/shared link (e.g. `/movies`) loads the app instead
  of 404ing.
- **1.79 (2026-08-28)** — Restored the hero swipe-transition between library
  tabs (the outgoing hero slides out one edge while the incoming one slides
  in from the other), the last piece recovered from the same stray source
  copy before it was deleted for good.
- **1.80 (2026-08-28)** — The video quality picker (Direct Play/1080p/720p/480p)
  is now a single dropdown chip instead of four separate buttons sitting in
  the player's chip row, matching the existing Audio/Subtitles chips.
- **1.81 (2026-08-28)** — Fixed transcoded playback (1080p/720p/480p) playing
  for a few seconds then jumping far ahead and stalling. The HLS playlist had
  no VOD marker while ffmpeg was still writing it, so hls.js treated it as a
  live stream and kept seeking to the "live edge" as GPU encoding raced ahead
  at 10-13x realtime. Added `-hls_playlist_type vod` so it's marked correctly
  from the first response.
- **1.82 (2026-08-28)** — 1.81's fix was wrong: `-hls_playlist_type vod` turned
  out to be incompatible with `-hls_flags append_list`'s incremental writes —
  ffmpeg held the entire playlist back instead of writing it as segments were
  produced (confirmed live: 145+ segments on disk, zero-byte playlist after
  25+ seconds), a worse break than the original bug. Switched to
  `-hls_playlist_type event`, the HLS type actually meant for a still-growing,
  eventually-complete (non-live) playlist — same fast incremental writes as
  before, but correctly tells the client up front that it isn't live.
- **1.83 (2026-08-28)** — 1.82 wasn't the full fix either: playback still
  started fine, then jumped far ahead and stalled. hls.js's "is this live"
  detection turns out to depend only on whether `#EXT-X-ENDLIST` is present
  (absent until ffmpeg finishes), not on `-hls_playlist_type` — so it kept
  seeking near the "live edge" regardless of the 1.82 change. Fixed at the
  actual point of control: `startPosition: 0` on the `Hls()` client config,
  since every transcode session's own HLS timeline always starts at zero
  and should never be "auto"-seeked.
- **1.84 (2026-08-28) — confirmed fix, verified live with the user watching
  the server logs in real time.** 1.83's `startPosition: 0` only controlled
  where hls.js started, not its ongoing behavior — since the stream was still
  classified `live` (no `#EXT-X-ENDLIST` until ffmpeg finishes), it kept
  re-syncing toward the "live edge" *during* playback too, not just at the
  start. Root cause, per hls.js's own issue tracker (video-dev/hls.js#668):
  that classification depends solely on ENDLIST being present, nothing else.
  Real fix: `/transcode/.../playlist.m3u8` now returns a **synthesized,
  complete VOD playlist** immediately — every segment the encode will
  eventually produce, computed from the file's known duration, with
  `#EXT-X-ENDLIST` present from the very first response — instead of
  ffmpeg's own playlist file, which structurally can't carry ENDLIST until
  encoding finishes. A segment requested before ffmpeg has actually written
  it now waits briefly instead of 404ing (normally well under a second at
  10-13x GPU encode speed). Verified with the user live: 8+ sequential
  segments, confirmed smooth playback well past the point that failed every
  previous round.
- **1.85 (2026-08-29)** — Added Quick Connect: sign in a keyboard-less device
  (the new TV app) by scanning a QR with an already-signed-in phone instead of
  typing a username/password/2FA code with a remote. New `quick_connect`
  table and three endpoints — `/api/auth/quickconnect/start` (public),
  `/poll/{token}` (public — the token in the path is the pairing's own
  credential), `/approve` (needs the phone's real session). The approved
  session token is handed back once, in a poll response body rather than a
  `Set-Cookie` header, since the device picking it up never made the request
  that would normally set one — see the new "Quick Connect" section above for
  the full flow. Verified end-to-end against a real SQLite DB and through a
  full FastAPI `TestClient` run (including the auth middleware actually
  gating `/approve` and the resulting session working against `/api/auth/me`)
  before this went anywhere near the live server.
- **1.86 (2026-08-29)** — The sidebar wordmark (`.brand`) is now a Home
  shortcut, click or Enter/Space, same as clicking a logo does on most sites.
  `role="button" tabindex="0"` on the div makes it keyboard-reachable; the
  click handler is just `activateTab("home")`, the same function every tab
  button already calls.
- **1.87 (2026-10-07)** — Glassmorphism restyle. Stylesheet only
  (`static/style.css`); no markup, script or API change. The page backdrop is
  now a teal-led colour wash, and the sidebar/top bar, Settings panels, title
  page fact panels, Live rows, search box and the sign-in card are frosted
  `backdrop-filter` panes over it. `--surface`, `--bg-2` and `--border` are
  translucent rather than hex, so pills, episode rows and inputs follow
  without their own rules; anything floating over video or key art (Back,
  Skip Intro, the quality/audio/subtitle menus) uses a darker tinted glass so
  it stays readable. Posters and the hero get a light rim and a drop shadow;
  solid teal buttons stay solid on purpose. Browsers without
  `backdrop-filter` fall back to near-opaque panels. Also fixes two older
  Settings defects found while checking the restyle: the two-factor
  "confirm password" and "scan the QR" steps were visible before being
  asked for (`.stack-form`'s `display:flex` beat the `hidden` attribute),
  and every row in the Libraries folder picker rendered as a solid accent
  pill (`.stack-form button` out-ranked the picker's own button rules).
  Verified by rendering Home, Browse, a movie page, a series page with
  episodes, Settings, the player, sign-in and the profile picker in headless
  Chromium against a throwaway container on a copy of the database.
- **1.88 (2026-10-07)** — "Get the apps" links. New optional `DOWNLOADS_URL`
  environment variable: the address of a page hosting the Android and TV app
  builds. When set, Settings gains a "Get the apps" panel (every account, not
  just admins) and the sign-in card a link under the form — in the card
  rather than the pitch column, because that column is hidden on a phone.
  Unset, which is the default, nothing is shown and nothing else changes.
  The value is served in `/api/public/stats` and `/api/settings` as
  `downloads_url`; anything not starting `http://` or `https://` is ignored,
  since it ends up in an `href` on a public page. The Android and TV apps
  ignore unknown JSON fields, so neither needs a rebuild.
- **1.89 (2026-10-18)** — Themes. Twenty new visual themes alongside the
  house style (Particles, Liquid Morph, Neon Glow, Y2K Chrome, Kinetic Type,
  Paper Cutout, Holographic, Isometric, ASCII Art, Clay 3D, Retro VHS, Comic
  Book, Split-Flap, Hand-Drawn, Blueprint, Physics, Pixel Art, Bauhaus, Art
  Deco, Neo-Brutalism), each with its own palette, fonts, card/button/panel
  treatments and, where it fits, motion. Chosen per profile in Settings →
  Theme; admins set the household default (also used on the sign-in page)
  under Appearance. New `PUT /api/profiles/current/theme`, a `theme` field in
  `/api/settings`, `/api/public/stats` and the profile in `/api/auth/me`, and
  a `profiles.theme` column (added automatically on start). Effects respect
  the system reduced-motion setting and can be turned off per device. The
  sign-in and profile pages now get the same `?v=` cache-busting as the app.
  The house style is unchanged when "HomeReel" is selected. The Android and
  TV apps ignore the new fields, so neither needs a rebuild.
- **1.90 (2026-10-18)** — Artwork for a second movie library. A second
  Radarr can now supply posters, ratings and cast through
  `RADARR_ANIME_URL` / `RADARR_ANIME_API_KEY`. Because artwork is matched by
  folder path, and that Radarr typically mounts its folder at its own
  `/movies` (already taken by the main library here), the new
  `RADARR_ANIME_PATH_MAP` rewrites its path prefix into HomeReel's
  (e.g. `/movies:/animemovies`) before matching. Nothing changes when the new
  variables are unset.
