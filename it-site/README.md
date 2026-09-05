# Macrodyne Inside

`index.html` is the internal home page for everyone at Macrodyne: the links
people use every day, a noticeboard, tips, and a ticker of what IT is working
on. The newsletter archive sits alongside it.

Self-contained HTML files. No framework, no build step, no bundler — each page
carries its own CSS and JS inline. The only moving part is one Azure Function
that reads four SharePoint lists.

```
it-site/
  site/                          <- everything served to the browser
    index.html                     the home page: links, noticeboard, tips, ticker
    no-access.html                 shown to anyone who lands somewhere they may not go
    staticwebapp.config.json       auth + who can reach what
    uptime/index.html              Uptime archive — lists every issue
    uptime/001/index.html          Issue 001
  api/
    bulletin/                      GET /api/bulletin
```

---

## The one thing to get right

**Everything in this folder is company-facing.** The work board, the priority
list and the value summary are not here, and must not be put here. Between them
they carry the support contract figures, the management plane position and the
security remediation detail. They live as private documents, not as pages on a
site that 130 people open every morning.

If a page like that is ever added, it goes in `site/internal/`, behind the
`/internal/*` route rule that is already in `staticwebapp.config.json` — as its
own file, never as a hidden section of a company page. A section hidden with CSS
or JavaScript is still in the file the browser downloaded; anyone who opens View
Source reads it. Route rules are the only thing that does real work.

### This repo also feeds a public site

`macrodyne-it-tools` already deploys to the **orange-bay** Static Web App with
`app_location: "/"`, which uploads the entire repository — with no sign-in in
front of it. Anything committed here would be readable by anyone with the URL.

So the orange-bay workflow carries one guard step:

```yaml
- name: Keep the internal home page out of the public app
  run: rm -rf it-site
```

**Do not remove it.** Without it, the whole company noticeboard is on the open
internet. This folder is deployed only by `.github/workflows/it-site.yml`,
which is scoped to `it-site/**`.

Two things worth knowing about that public app while you are in here: it also
serves the Requisition app's Python source (`requisition/*.py`) as static files,
because `app_location` is the repository root. No credentials are in those files
— they read environment variables — but the source is downloadable. Narrowing
`app_location` to a folder holding only the files meant to be served would close
both issues at once. That is a change to a live app, so it is noted here rather
than made.

---

## Updating the Bulletin — the normal case

Add a row to the **Bulletin** list in SharePoint, from the SharePoint or Teams
app on your phone. The page picks it up within a minute. No commit, no deploy.

That is the whole point of the arrangement: the moment you most need to post a
notice is the moment you are least able to edit a file and wait for a build.

### The list

Site: `https://macrodyne.sharepoint.com/sites/<your IT site>`
List name: `Bulletin`

| Column        | Type                | Notes                                                        |
| ------------- | ------------------- | ------------------------------------------------------------ |
| `Title`       | Single line         | The headline. Plain English, no jargon.                       |
| `Status`      | Choice              | `live`, `planned`, `notice`, `resolved` — exactly these words |
| `EventDate`   | Date and Time       | Drives the ordering and the displayed date                    |
| `Summary`     | Multiple lines      | Plain text. Two sentences is plenty                           |
| `WhatsDown`   | Single line         | live / planned                                                |
| `Why`         | Single line         | live / planned — **the field that says "this is not us"**     |
| `BlastRadius` | Single line         | who it hits                                                   |
| `BackBy`      | Single line         | live / planned. Write it even if it is "no estimate yet, next update 11:00" |
| `Action`      | Single line         | notices — what to do                                          |
| `Outcome`     | Single line         | resolved — e.g. "Down 47 minutes · back at 14:52"             |
| `Department`  | Choice              | Who posted it — `IT`, `HR`, `Operations`, `Finance`, `Health & Safety`. Defaults to `IT` |

Only `Title`, `Status`, `EventDate` and `Summary` are needed on every row. The
rest fill in as they apply, and empty ones are left off the page.

### Who can post

This is the decision that turns an IT page into a company home page, and it is
a SharePoint permission, not a code change: give write access on the `Bulletin`
list to the people who should be able to post, and set `Department` on their
notices so a reader can see who is speaking.

Two things worth settling before the link goes out:

- **More than one person per department.** A noticeboard with a single
  keyholder goes quiet the first week that person is away, and a quiet home
  page reads as "nothing is happening" rather than "nobody posted".
- **Nobody approves posts.** There is no workflow here on purpose — a notice
  that has to be approved arrives after the thing it was warning about. The
  control is that every notice carries a name and a department.

Anything `live` floats to the top regardless of date.

**Make a list view for the phone** with just Title, Status and EventDate
visible. Posting a notice should take under a minute standing in a corridor.

### When something breaks

The page has two fallbacks, in order:

1. the last copy this browser saw, with a note saying how old it is;
2. the standby entries written into `site/index.html`, with a note saying so.

This matters more than it looks. The bulletin's busiest day will be a day
Microsoft is having a bad one — and if the notice list is in SharePoint, it can
be unreachable at exactly that moment. Keep one or two sensible standby entries
in the file so the page never renders empty.

---

## Tips and the ticker — also lists

Same arrangement as the notices. Nothing that changes regularly lives in a file.

### `Tips`

| Column      | Type          | Notes                                                   |
| ----------- | ------------- | ------------------------------------------------------- |
| `Title`     | Single line   | The tip itself, as an instruction                        |
| `App`       | Choice        | `Outlook`, `Teams`, `Both`, `Windows`                    |
| `Body`      | Multiple lines| One sentence on why anyone would care                    |
| `How`       | Single line   | Optional. Square brackets become keys: `[Ctrl] + [Shift] + [M]` |
| `SortOrder` | Number        | Low first                                                |
| `Active`    | Yes/No        | Untick to retire one without deleting it                 |

Anyone can be given access to add to this. A tip that arrives from a colleague
gets read more than one from IT, so put their name in the `Body`.

### `Workstreams` — the ticker

| Column      | Type        | Notes                                        |
| ----------- | ----------- | -------------------------------------------- |
| `Title`     | Single line | One line. No dates, no figures               |
| `State`     | Choice      | `now`, `next`, `done`                        |
| `SortOrder` | Number      | Low first                                    |
| `Active`    | Yes/No      | Untick when it stops being interesting       |

**Updating the ticker is changing one dropdown from `now` to `done`.** That is
the whole point: an item that reads `now` for four months does more harm than
leaving it off, and the only way that does not happen is if fixing it takes ten
seconds from a phone.

### `Links` — the row of tiles at the top

Optional. Leave `LINKS_LIST_ID` unset and the page uses the tiles written into
`site/index.html`; set it and the list wins.

| Column        | Type        | Notes                                             |
| ------------- | ----------- | ------------------------------------------------- |
| `Title`       | Single line | What it is called — `Email`, `ETO`, `Time and pay` |
| `Description` | Single line | Four or five words on what it is for              |
| `Url`         | Hyperlink   | Must start `http://` or `https://` — anything else is dropped |
| `SortOrder`   | Number      | Low first. Put the three most-used first          |
| `Active`      | Yes/No      | Untick to retire one                              |

A tile with no address renders amber and does nothing, rather than sending
someone to a dead page. That is why the page ships with several amber tiles:
those addresses have not been supplied yet.

## Updating the newsletter

Issues live one folder each: `site/uptime/001/index.html`.
  For Issue 002, copy `001/` to `002/`, write it, then add a block to the
  archive index at `site/uptime/index.html` and update the issue number in
  the ticker link on `site/index.html`. Two small edits, both marked with a
comment in the file.

That one is a file edit — a designed document, written once a month, not a data
record. Commit to `main` and the workflow deploys in about ninety seconds.

---

## First-time setup

### 1. Static Web App

Create one (Free tier is enough) in Canada Central, linked to this repository.
Take the deployment token it gives you and add it to the repo as the secret
`AZURE_STATIC_WEB_APPS_API_TOKEN_IT_SITE`.

The workflow is already scoped to `it-site/**`, so it will not fire on
Requisition App commits.

### 2. Entra sign-in

`staticwebapp.config.json` already points at the Macrodyne tenant. Register an
app for the site (or reuse an existing one), add the callback
`https://<your-swa>.azurestaticapps.net/.auth/login/aad/callback`, and set two
application settings on the Static Web App:

```
AAD_CLIENT_ID       <the app registration client id>
AAD_CLIENT_SECRET   <a client secret>
```

Everyone in the tenant then gets the Bulletin. Nobody outside it does.

### 3. Roles

Nothing here needs one. Everyone in the tenant gets the home page; nobody
outside it does. The `/internal/*` rule exists so that a future restricted page
is gated by default, not because anything is behind it today.

If such a page is ever added: Static Web Apps roles are per-user by invitation
on the Free tier, which is fine for two or three people. Group-driven roles need
the Standard tier and a roles function — **confirm the current tier behaviour
against Azure's docs before planning around it**, this is the detail in this
README most likely to have moved.

### 4. The bulletin function

Register an app for it (separate from the sign-in one), then:

- Graph **application** permission `Sites.Selected`, admin consented.
- Grant that app **read** on the one site holding the list:

  ```
  POST https://graph.microsoft.com/v1.0/sites/{site-id}/permissions
  { "roles": ["read"],
    "grantedToIdentities": [{ "application": { "id": "<client-id>",
                                               "displayName": "IT site bulletin" }}] }
  ```

  `Sites.Selected` rather than `Sites.Read.All` on purpose: this credential can
  read that one site and nothing else in the tenant. If it leaks, the blast
  radius is a list of IT notices.

- Find the ids:

  ```
  GET /sites/macrodyne.sharepoint.com:/sites/<site>            -> site id
  GET /sites/{site-id}/lists?$filter=displayName eq 'Bulletin' -> list id
  ```

- Set on the Static Web App:

  ```
  BULLETIN_TENANT_ID      4c9a50a1-c27f-4044-8025-b59b5b804d16
  BULLETIN_CLIENT_ID
  BULLETIN_CLIENT_SECRET       <- put a calendar reminder on its expiry
  BULLETIN_SITE_ID
  BULLETIN_LIST_ID
  TIPS_LIST_ID
  WORKSTREAMS_LIST_ID
  LINKS_LIST_ID                <- optional
  ```

### 5. SharePoint

Put a link to the site on the IT SharePoint page.

A link, not an embed, at least to begin with. The Embed web part iframes the
page, and a first-time visitor who is not yet signed in gets an Entra redirect
inside an iframe, which Microsoft's login blocks. Once people have a session it
works — but the first impression is a blank frame, and first impressions are
what this whole exercise is about.

If you do embed it later, `staticwebapp.config.json` already permits framing
from `*.sharepoint.com` and Teams.

---

## Making it everyone's home page

Three separate things, and they are worth doing in this order rather than
together.

**1. A name people can type.** `orange-bay-1234.azurestaticapps.net` is not a
home page. Add a custom domain on the Static Web App — `inside.macrodyne.com`
or similar — with the CNAME on the public DNS. Free tier supports custom
domains. Everything below assumes that name exists, because changing the
address after it has been pushed to 130 browsers is the one step that cannot be
undone quietly.

**2. Pin it in the places people already are.** Cheap, reversible, and it tells
you whether anyone reads it before you touch anyone's browser:

- a link on the SharePoint intranet home;
- a Teams tab in the all-company team (Website tab, pointing at the custom
  domain);
- the link in the monthly Uptime email.

**3. Only then, set it as the browser home page.** Edge and Chrome both take a
policy for this, applied through the management plane — which for the Windows
estate means WFTF, since they hold the endpoint tooling. The relevant settings:

| Browser | Policy                                        | Effect                     |
| ------- | --------------------------------------------- | -------------------------- |
| Edge    | `RestoreOnStartupURLs` + `RestoreOnStartup=4` | Opens on launch            |
| Edge    | `HomepageLocation`                             | The home button            |
| Edge    | `NewTabPageLocation`                           | Every new tab              |

Set the startup page and the home button. **Think hard before taking the new
tab page** — a person who opens thirty tabs a day will resent it, and resentment
attaches to the page, not to the policy.

Two things to settle with WFTF before that request goes in:

- Do it as a *set default*, not a lock. A policy people cannot change turns the
  page into something done to them.
- Confirm what happens on a machine that opens before the VPN or the network is
  up. A home page that fails to load on launch is worse than no home page — and
  because sign-in is Entra, an offline launch will land on a Microsoft error
  rather than on anything of ours.

**What it does not need.** Sign-in is already tenant-wide: everyone in Entra
gets the page, nobody outside it does. No new group, no new licence, no
per-person setup.

---

## Local preview

```
npx @azure/static-web-apps-cli start it-site/site --api-location it-site/api
```

Without the environment variables set, `/api/bulletin` returns 503 and the page
falls back to its standby entries — which is a useful thing to look at anyway,
since it is what people will see on a bad day.

---

## Before it goes live

Everything amber on the page is an address nobody has supplied yet. Nothing
below needs code; all of it needs a decision or a URL.

**Addresses**

- [ ] Helpdesk route — address or extension, in `site/index.html`
- [ ] Where to send tips — same file
- [ ] Uptime link at the foot of the ticker — set the `href`, drop `needs-url`
- [ ] The six amber tiles: Files, ETO, Project Console, Requisition form,
      Time and pay, Get IT help
- [ ] Project Console and ETO Web addresses in `site/uptime/index.html`

**Content**

- [ ] Replace the sample notices with real ones — a home page that opens on a
      stale example is worse than no home page
- [ ] Remove the amber draft notes from both pages

**People**

- [ ] Decide who can post, per department, and give them write access on the
      `Bulletin` list. At least two people, not one
- [ ] Show those people the phone view once. It takes five minutes and it is
      the difference between a page that lives and a page that does not

**Safety**

- [ ] Confirm the guard step is in the orange-bay workflow, then check that
      `https://orange-bay-0e0de3210.7.azurestaticapps.net/it-site/site/index.html`
      returns 404 — in a private window, signed out
- [ ] Confirm the new site prompts for sign-in in a private window, and that an
      account outside the tenant cannot get in
- [ ] Confirm the page still renders when `/api/bulletin` is down (stop the
      function, reload) — that is the state it will be in on the worst day
