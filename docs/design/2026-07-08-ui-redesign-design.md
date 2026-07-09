# UI Redesign Design

Date: 2026-07-08

## Goal

Redesign `tg-downloader-ui` as a media download control center that works
equally well for Docker, iStoreOS, OpenWRT, and ordinary server installs.

The redesign should make the product feel like a long-running media download
and archive console, not a router-only administration page.

## Approved Direction

Use the "media library control center" direction.

The UI should emphasize:

- download queue status
- media titles and archive paths
- progress, speed, and job state
- source management
- the split between Telethon monitoring authorization and `tdl` download
  authorization

The visual tone should be calm, durable, and operator-friendly. It should avoid
marketing-page styling and avoid making the app look tied only to OpenWRT.

## Existing Stack

The app has no frontend framework. The current UI is embedded in
`tg_downloader_ui/app.py` as:

- `INDEX_HTML`
- `SETUP_HTML`
- `LOGIN_HTML`

Styling is vanilla CSS inside each HTML string. Behavior is native JavaScript.

This is intentional for low-dependency router/server deployment. The redesign
must keep this model:

- no frontend build step
- no CDN dependency
- no npm dependency
- no new runtime dependency
- no API migration

## Non-Negotiable Compatibility

Keep existing IDs, route names, and JavaScript function names that tests and
runtime behavior rely on, including:

- `sourceSelect`
- `messageIds`
- `jobsBody`
- `logPanel`
- `telegramQr`
- `tdlQr`
- `tdlLoginOutput`
- `pauseJob`
- `resumeJob`
- `/api/jobs/{id}/pause`
- `/api/jobs/{id}/resume`
- Telegram and `tdl` login endpoints

Keep the existing Chinese admin-console copy that current tests assert. New or
rewritten copy should also be Chinese where it is user-facing.

The setup page must be converted from English to Chinese while preserving its
form IDs and setup submission behavior.

## Page Structure

### Main Shell

Keep the current five-section structure:

- download tasks
- path settings
- source settings
- Telegram authorization
- password management

Use a media-service brand treatment instead of an OpenWRT-specific one.
Recommended visible brand copy:

- `TG 下载中控`
- subtitle: `媒体归档服务`

The sidebar remains because it is simple and stable. It should feel lighter and
more polished through spacing, color, active states, and typography.

### Downloads Page

Reframe the page as a workbench:

1. Submit area
   - source selector
   - message ID textarea
   - submit button
   - inline submit message for errors or success
2. Status overview
   - active job
   - queued count
   - completed count
   - failed count
   - paused count
   - forwarder state
3. Download queue
   - keep the table for density
   - improve status badges, progress bars, title/path cells, and action buttons
4. Task output
   - keep the log panel
   - style it as a consistent terminal/output surface

Do not replace the job table with cards in the production UI. The current data
density is useful and table behavior is safer for this phase.

### Path Settings

Keep the path input, directory browser, and save action. Style the section as a
settings panel with a clearer label hierarchy and inline message state.

### Source Settings

Keep editable source rows. Do not introduce modal editing or card-only source
management. Source editing should remain fast for operators.

### Telegram Authorization

Keep the two-login mental model visible:

- `Telethon 用户授权`: monitoring and message handling
- `tdl 下载授权`: actual Telegram file downloads

Each authorization area should have clear internal groups:

- configuration
- code login
- QR login
- output/status

Make it visually obvious that Telethon session state does not automatically log
in `tdl`.

### Login Page

Use the same visual language as the main app:

- Chinese title
- media download control center brand
- focused login panel
- inline error state

Do not add external marketing content.

### Setup Page

Convert setup to Chinese and align with the new visual system.

Recommended copy:

- title: `初始化下载中控`
- admin username: `管理员账号`
- admin password: `管理员密码`
- download directory: `下载目录`
- Telegram API ID: `Telegram API ID（仅转发器需要）`
- Telegram API hash: `Telegram API hash（仅转发器需要）`
- session file: `Telegram Session 文件（仅转发器需要）`
- forward channel ID: `转发目标频道 ID（仅转发器需要）`
- submit button: `完成初始化`

Keep the existing setup payload and redirects.

## Visual System

Use CSS variables in each page or a shared copied block inside each embedded
HTML string.

Recommended palette:

- background: warm light gray
- panel: white or warm off-white
- alternate surface: slightly tinted gray
- line: soft warm gray
- text: near-charcoal
- muted text: balanced gray
- accent: steel teal or deep green
- success: muted green
- warning: muted amber
- error: muted red
- log surface: deep charcoal with a green tint

Avoid purple/blue gradients, pure black backgrounds, and highly saturated
accents.

Typography:

- Use local system fonts only.
- Prefer `system-ui`, `Segoe UI`, `PingFang SC`, `Microsoft YaHei`,
  `Noto Sans SC`, Arial, sans-serif.
- Use semibold weights for labels and navigation.
- Use monospace/tabular numbers for IDs, counts, progress, speed, PID, and log
  output.
- Keep letter spacing at `0` for normal Chinese copy.

Layout:

- Keep a maximum content width for large screens.
- Keep table horizontal scrolling on small screens.
- Use stable dimensions for buttons, status badges, metrics, progress bars, and
  log surfaces.
- Avoid nested cards.

## Interaction And States

Add or improve:

- `hover`, `active`, and `focus-visible` states for buttons, nav items, inputs,
  select boxes, directory rows, and table rows
- disabled button styling
- inline submit error message instead of `alert()` where practical
- empty state for the jobs table
- clearer message styling for success, muted status, and errors
- smooth but minimal transitions around color, border, shadow, and transform

Do not add animation that could make the admin console feel unstable.

## Prototype Step

Before modifying the production UI in `tg_downloader_ui/app.py`, create a
static HTML prototype.

Prototype requirements:

- place it under `docs/prototypes/`
- no external network assets
- no build step
- use static sample data only
- include the main dashboard, login page, and setup page in one prototype file
  or closely linked files
- demonstrate desktop and mobile-responsive behavior
- keep copy in Chinese
- show realistic media download sample rows, states, and logs
- include empty/error states where useful

The prototype is for visual review only. It must not call real APIs or include
secrets.

After the prototype is approved, map the prototype styles and markup changes
back into the embedded HTML templates.

## Verification Plan

After implementation, run:

```sh
python -m unittest discover tests -v
python -m compileall tg_downloader_ui tests
python tg_downloader_ui/app.py --check
```

If local Python is unavailable on the Windows host, use the existing OpenWRT
test container path described in `docs/HANDOFF.md`.

For visual verification, start the app or open the static prototype and inspect:

- desktop layout
- mobile layout
- login page
- setup page
- downloads page with jobs
- downloads page empty state
- Telegram authorization page
- directory browser modal

## Out Of Scope

This redesign does not include:

- API changes
- download worker changes
- database changes
- packaging changes
- adding a frontend build tool
- adding a component framework
- replacing the job table with a card-only layout
- adding real media posters or external image assets
