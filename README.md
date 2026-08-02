# Edinburgh Festival Review Leaderboard

Reads Edinburgh Festival reviews once a day, pulls out the star ratings, and
publishes a leaderboard as a website.

**Ranking:** Olympic-style on 4- and 5-star reviews only. One 5-star review
outranks any number of 4-star reviews. Lower ratings are shown for reference but
never affect a show's position.

---

## Running it on your own Mac

You only need to do steps 1-2 once.

**1. Set up Python** (one time)

Open Terminal, then paste these one at a time:

```bash
cd ~/fringe-leaderboard
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

**2. Try it on real data from last year's festival**

```bash
./.venv/bin/python run.py --backfill 2025-08 --reset
```

That takes a few minutes (it deliberately waits a second between requests to be
polite to the websites). When it finishes:

```bash
open site/index.html
```

**3. Normal daily run**, once the festival is on:

```bash
./.venv/bin/python run.py
```

### Other commands

| Command | What it does |
|---|---|
| `run.py` | Normal run: fetch new reviews, match, rebuild the page |
| `run.py --backfill 2025-08` | Read an old month's archive instead of the live feed |
| `run.py --limit 5` | Only process 5 items per site — fast, for testing |
| `run.py --render` | Just rebuild the HTML from what's already stored |
| `run.py --match` | Just redo show matching, after you've fixed an alias |
| `run.py --reset` | Delete the database and start from scratch |

---

## Putting it online (GitHub Pages)

**1. Make a GitHub account** at github.com if you don't have one.

**2. Create the repository.** Click the green **New** button. Name it
`fringe-leaderboard`. Choose **Public** — GitHub Pages is free on public repos.
Don't tick "Add a README", you already have one.

**3. Install GitHub Desktop** (desktop.github.com). Sign in. Choose
`File → Add Local Repository` and pick your `fringe-leaderboard` folder. You'll
see all your files listed as changes. Write "First commit" in the box at the
bottom left, click **Commit to main**, then **Publish repository**.

> From now on, "pushing a change" means: make your edit, open GitHub Desktop,
> type a short description, click **Commit to main**, then **Push origin**.

**4. Turn the website on.** In your repo on github.com go to
**Settings → Pages**. Under *Source* choose **Deploy from a branch**. Set Branch
to `main` and folder to `/site`. Click **Save**.

Two minutes later your leaderboard is live at:

```
https://jamierd212.github.io/fringe-leaderboard/
```

**5. Turn the daily robot on.** Go to the **Actions** tab and enable workflows if
prompted. You'll see *Daily review sweep*. Click it, then **Run workflow** to
test it immediately rather than waiting until 8am.

### Before you go live

- In `sources.yaml`, put your own GitHub URL in the `user_agent` line so
  publications can see who's requesting their pages.
- In `templates/index.html.j2`, replace `YOUR-EMAIL-HERE` with a real address so
  publications can report mistakes.

### Things that will trip you up

- **GitHub cron is UTC.** `0 7 * * *` is 08:00 BST in summer. In winter that
  same line fires at 07:00.
- **Scheduled runs can be 5-20 minutes late.** They queue. Don't promise anyone
  08:00 exactly.
- **GitHub switches off scheduled workflows after 60 days of no repo activity.**
  Fine during the festival; expect to re-enable it each summer.

---

## Daily upkeep during the festival

Open `review_queue.md`. It lists matches the robot wasn't fully confident about.
Usually they're fine. Occasionally you'll see the same show listed twice under
slightly different names — that's a *false split*.

To merge them, find the correct `show_id` and add the wrong spelling as an alias:

```bash
./.venv/bin/python - <<'EOF'
from src import db
conn = db.connect()
conn.execute("INSERT OR IGNORE INTO aliases (alias, show_id) VALUES (?, ?)",
             ("achilles", "2025-achilles-death-of-gods"))
conn.commit()
EOF
./.venv/bin/python run.py --match
```

Realistically this is a few minutes over coffee.

---

## How it works

```
sources.yaml ──▶ collect.py ──▶ ratings.py ──▶ match.py ──▶ rank.py ──▶ render.py
                 find new       pull out       which          Olympic     static
                 review URLs    the stars      show is it?    ranking     HTML
```

| File | Job |
|---|---|
| `sources.yaml` | The publications list. **The file you'll edit most.** |
| `src/collect.py` | Reads RSS feeds, filters news and non-Edinburgh shows |
| `src/ratings.py` | Finds a star rating; converts other scales to stars |
| `src/normalise.py` | Turns messy headlines into comparable show titles |
| `src/match.py` | Decides which show a review belongs to |
| `src/rank.py` | The leaderboard maths |
| `src/render.py` | Builds the HTML page |
| `data/reviews.db` | Everything found so far (committed daily = free history) |

### How ratings on other scales are handled

Publications don't all use five stars. Anything on a different scale is converted:

| Publication gives | Becomes | Shown on the site as |
|---|---|---|
| `4/5` | 4★ | — |
| `80%` or `8/10` | 4★ | `80%` |
| `90%` or `9/10` | 4★ | `90% rounded down` |
| `3.5***` | 3★ | `3.5/5 rounded down` |
| "Highly Recommended" (FringeReview) | 4★ | `Highly Recommended` |
| "Daring Work" (FringeReview) | *not counted* | — |

**Ties round down, always.** A publication giving 9/10 has said "very good but
not perfect"; promoting that to five stars would put a show at the top of the
leaderboard on the strength of a rating nobody gave it. Rounding down can only
ever understate a show, which is the safe direction of error when 5-star counts
decide the ranking. The original figure is always stored and displayed, so
nothing is hidden.

### FringeReview's word grades

FringeReview dropped star ratings deliberately and awards named badges instead.
Its seven **quality** badges are mapped to stars:

| Badge | Stars |
|---|---|
| Outstanding Show, Must See Show, Excellent Show | 5★ |
| Highly Recommended, Very Good Show | 4★ |
| Recommended, Good Show | 3★ |

Its four **descriptive** badges — Daring Work, Exciting Work, Groundbreaking
Work, Hidden Gem — are **not** mapped. They describe the kind of work rather
than its quality, so there's no honest star value; those reviews are skipped.

Two things to keep in mind:

- FringeReview states there is "deliberate overlap" between its ratings and
  rejects a strict hierarchy. **The numbers are our editorial call, not theirs**,
  which is why the badge name is always shown next to the stars on the page.
- They only publish shows rated "Good" or better — weaker shows get private
  feedback instead. So FringeReview can lift a show up the leaderboard but never
  pull one down. In the 2025 test data this mattered less than expected: 22% of
  their reviews landed at 5★ against 20% for the other publications.

To change the mapping, edit `FRINGEREVIEW_BADGES` in `src/ratings.py`, then run
`run.py --reset` to re-score everything.

---

## Adding a publication

Add an entry to `sources.yaml`:

```yaml
  - name: Example Reviews
    slug: example
    homepage: https://example.com/
    feed: https://example.com/feed/
    rating:
      type: auto          # try structured data, then ★, then "4/5", then "four stars"
    fetch_page: false     # true if the rating isn't in the feed itself
    pages: 2              # how many pages of the feed to walk (10 items each)
```

Most WordPress sites expose `/feed/`. Test with
`run.py --limit 3` and see whether ratings come through.

If the rating only exists as page markup, point at it directly:

```yaml
    rating:
      type: css
      selector: "span.rating .value"
      scale: 100          # the publication's own maximum
    fetch_page: true
```

Set `enabled: false` with a `notes:` line rather than deleting a publication, so
you don't re-investigate it in six months.

---

## Being a good citizen

- Requests are rate-limited to one per second per site and identify themselves.
- Only the rating, headline and link are stored — never the review text.
- Every rating links back to the publication.
- Sites that block bots (Broadway Baby returns 403) are left disabled. Ask them
  for permission rather than working around it.
- There's a corrections link in the page footer. Keep it working.

## Licence

All rights reserved — see [LICENSE](LICENSE). The repository is public so the
method is open to inspection, and because GitHub Pages requires a public repo on
a free account. That is not a grant of permission to reuse the code or the
collated database.

The star ratings themselves belong to the publications that awarded them; this
project only counts them and links back. To permit reuse instead, replace
LICENSE with an MIT licence.

## Not built yet (Phase 2+)

- **AI fallback** for pages where the rules find nothing, and for the ambiguous
  show matches now going to `review_queue.md`. "Achilles" and "Achilles, Death of
  the Gods" are the same show, and no amount of fuzzy string matching will
  reliably tell you that.
- **Edinburgh Festivals Listings API** as the canonical show list. Free, but live
  Fringe data needs email approval and development against a `demofringe`
  dataset first — worth starting early.
- Genre and venue filtering, "new today" highlighting.
