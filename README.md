# Fantasy GM

A free, local dashboard for fantasy football (Sleeper) and fantasy basketball (ESPN). It sets your lineup, explains every suggestion, finds waiver moves and trades that actually help your roster, shows your matchup and win chance, and pulls news on your players. It cross-checks its own suggestions against outside sources.

It uses only Python's standard library: nothing to install, no API keys, no paid services.

## What you need installed

Only two things, on either Mac or Windows:

1. **Python 3.8 or newer**
2. **A web browser** (Chrome, Safari, Edge or Firefox)

No extra packages, so you do not need `pip`, Anaconda, Node or anything else. You need an internet connection while the app is running.

### Mac

1. Open **Terminal** (Applications > Utilities) and run `python3 --version`. If it prints `Python 3.8` or higher, you are done. Anaconda's Python works too.
2. If the command is not found, or macOS offers to install "command line developer tools", accept that prompt, or download the macOS installer from https://www.python.org/downloads/.
3. If you installed from python.org, open the new **Python 3.x** folder in Applications and double-click **Install Certificates.command** once. Without it the app shows "Certificate check failed".

### Windows

1. Open **Command Prompt** (Windows key, type `cmd`, Enter) and run `python --version`. If it prints `Python 3.8` or higher, you are done.
2. If it is not found, or the Microsoft Store opens, download the installer from https://www.python.org/downloads/. **Tick "Add python.exe to PATH"** on the first screen before clicking Install, then reopen Command Prompt.
3. If `python` still is not recognized, try `py --version` and use `py` in place of `python` below.

## Starting it

### Mac

Double-click `Start Fantasy GM.command`. Or in Terminal:

```
cd path/to/the/folder
python3 fantasy_gm.py
```

Tip: type `cd ` (with a space), drag the folder from Finder into Terminal, and press Enter.

If macOS says you do not have access privileges, run `chmod +x "Start Fantasy GM.command"` once from inside the folder. If it says the file is from an unidentified developer, right-click it and choose **Open**.

### Windows

The `.command` launcher is Mac-only. In Command Prompt:

```
cd path\to\the\folder
python fantasy_gm.py
```

Tip: in File Explorer, click the folder's address bar, type `cmd` and press Enter.

### Both

The dashboard opens at http://127.0.0.1:8765 and runs until you close the Terminal or Command Prompt window (or press Ctrl+C). Options: `--port 8766`, `--no-browser`.

## First-time setup

The first time you open it, Fantasy GM goes straight to the setup screen. Do either step or both.

### Step 1: Football on Sleeper

Type your Sleeper username and press **Find my leagues**. No password. Every league you are in this season appears in the league menu.

### Step 2: Basketball on ESPN

1. Open your team at fantasy.espn.com, copy the address from the top of the browser, and paste it in. Fantasy GM reads the league, season and team from the link.
2. If the league is public, that is all. If it did not find your team, pick it from the list.
3. If the league is private, you get three choices:
   - **Find my ESPN sign-in** (easiest). If you are signed in to ESPN in Chrome, Edge, Brave or Firefox on this computer, the app reads that sign-in for you. On a Mac, a system box asks permission to read the browser's saved sign-in: enter your Mac password and choose Allow. On Windows this works with Firefox only. Safari is not supported.
   - **Do it by hand.** The screen walks you through copying two values (`espn_s2` and `SWID`) from your browser. You can paste them in any form; the app cleans them up.
   - **Make the league public.** Ask your league manager to turn on "Make League Viewable to Public" in League Settings > Basic Settings. No sign-in is needed after that.

ESPN sign-ins expire now and then. If basketball stops loading, repeat the sign-in step.

### Step 3: Voices you trust

YouTube channels whose opinion should weigh on every suggestion. It starts with Upper Hand Fantasy. Add others separated by commas (a handle like `@ChannelName` or a pasted channel link), or clear the box to turn it off. Settings shows whether each channel was found and how many videos from the last 9 days it will read.

## What you see

**Matchup card (top).** Your projected score against this week's opponent, a win-chance ring, where each side has the edge, and a slot-by-slot (football) or day-by-day (basketball) breakdown. In football it also shows how much your win chance improves if you make the suggested lineup changes.

**Lineup.** The best legal lineup. Tap any player to see why: his projection from each source, season average, expert rank, how often he is started, who the next best option is and by how much. Each starter gets a label:

| Label | Meaning |
|---|---|
| Clear start | Comfortably ahead of the alternative, and the sources agree |
| Lean start | Ahead, but not by much |
| Toss-up | Too close to call, or experts prefer the other player |
| Check status | Questionable or day-to-day; confirm he is playing |

Bench players explain why they sit (ruled out, no game, or who is ahead of them).

**Waiver wire.** Opens with a roster check, then two lists:

- **Short term** (this week in football, the next 7 days in basketball): bye-week fill-ins, injury cover and one-week streamers. Each says what the drop costs you later, so you can tell a free upgrade from a rental.
- **Long term**: moves that improve your lineup or usable depth over the weeks that matter most to you (see Outlook).

If nothing helps, it says so.

**Trades.** Deals that make your team better and still give the other manager a reason to say yes. They do not have to be even: each one says which way it tilts on paper and is labeled "Likely to accept", "Worth an offer" or "Long shot". Includes two-for-one offers, where you package two useful players for one better starter and fill the open spot from the waiver wire. Each is tagged "Win-now deal", "Playoff-push deal" or "Helps now and later", with the points it adds this week and in your playoff weeks. Deals that work against your situation are left out (for example, one that costs you points this week when you need wins now).

**Outlook.** Your record, standing and estimated chance of making the playoffs, what that means for your moves, the league standings, and a schedule grid for every player on your roster: bye week, the next few opponents and your fantasy playoff opponents, colored by how soft or tough each matchup is for his position. Basketball shows games in the next 7 days and the 7 after.

**Tracker.** How the lineup advice has actually turned out.

- **Summary tiles:** the share of start-or-sit calls that were right, points scored by the recommended lineup against the best possible lineup in hindsight, points left on the bench per week, and (football) the recommended lineup against the lineup you actually set.
- **Each week or day:** tap it to see every call (who was started, the best alternative, both scores, right or wrong), every bench player who outscored a starter and by how much, and where your own lineup differed from the recommendation.
- **How a call is judged:** a starter chosen over the best bench alternative for that slot is "right" if he scored at least as many points.
- **Football:** weeks the app was open for are marked "tracked" and use the exact lineup it recommended before kickoff (the recommendation is saved each time you open that week, until the first game starts). Earlier weeks are marked "rebuilt": they show what that week's projections alone would have picked, so the record is not empty on day one.
- **Basketball:** graded day by day, and only for days you opened the app, since that is when a recommendation is saved. Results appear the next day.

**Trusted voices.** What your trusted YouTube channels said this week about your players and about players available in your league, each with a short excerpt and a link to the video (to the exact moment when captions were readable), plus the list of videos checked.

**News.** ESPN fantasy notes plus a Google News search for each of your players, injured players first, and a search box.

**Roster.** Every number behind each player.

**Copy briefing.** Copies a text summary to paste into a chat assistant for a second opinion. `python3 fantasy_gm.py --briefing nfl` (or `nba`) prints it in Terminal.

## How it decides

**It values players by what they do for your roster, not by raw points.**

- Team strength is your best starting lineup, plus a small credit for useful depth.
- A bench player only counts for how much better he is than what is free on the waiver wire at his position, and only up to the number of backups that position can use. A third quarterback in a one-QB league, or a second linebacker when one starts, is worth close to nothing. Kickers and defenses get no bench credit.
- Waiver drops are whoever your roster would miss least after the pickup, and a pickup replaces a player at his own position whenever that costs nothing. A better linebacker replaces your linebacker; he never costs you a receiver.
- A position with a single lineup slot (QB in a one-QB league, kicker, defense, one linebacker) gets no credit for a stored backup, so players there are only suggested when they would start.
- A player eligible at two positions (for example DB/LB) is judged at the position your league actually starts.
- A player with nothing projected for the next three weeks is heavily discounted, whatever his season average says.
- The waiver page opens with a roster check showing where each of your lineup spots ranks in your league, with weak spots highlighted.
- Trades compare players by points above a free replacement, so a quarterback's big raw total does not make him worth a starting running back. You may receive up to about 45% more value than you send, or overpay by about 35%, as long as both lineups improve; the second player in a two-for-one counts for half to the side receiving him.

**Schedule and bye weeks.** The NFL schedule comes from ESPN's scoreboard (with Sleeper's listed opponents as a fallback).

- A player on bye is benched with the reason "On bye this week", and the short-term waiver list looks for someone to cover him.
- Every future week counts as zero for a player on bye and is nudged up or down by that week's opponent, so a player with a bye or tough matchups in your playoff weeks is worth less to you than his average suggests.
- Upcoming byes are called out in the "why", with a warning when one falls in your fantasy playoffs.

**Short term versus long term, based on your standings.** The app reads your league's playoff settings and standings, then plays out the remaining schedule 3,000 times (each team's current strength plus normal week-to-week luck) to estimate your chance of making the playoffs. That sets how the long-term value of every player is weighted:

| Situation | Playoff chance | Weight on the run-in | Weight on playoff weeks |
|---|---|---|---|
| In strong shape | 85% or more | 30% | 70% |
| On the bubble | 35% to 85% | 60% | 40% |
| Need wins now / long shot | under 35% | 90% | 10% |
| Playoffs under way | n/a | 0% | 100% |

The playoff chance is an estimate. It ignores tiebreakers beyond points scored, any league-specific rules such as a median game, and roster changes still to come.

**Football lineup number.** Sleeper's projection scored with your league's settings (60%) and ESPN's projection adjusted to your scoring (40%), steadied slightly by season average, reduced 8% for Questionable, zero if ruled out.

**Football rest-of-season value.** Half the next three weeks of projections, half season points per game. Injured-reserve players are discounted.

**Basketball value.** 40% season fantasy points per game, 35% last 15 days, 25% ESPN projection, then discounted by up to 30% for missed games. Minutes, usage and games played are shown beside it.

**Basketball waivers** are judged by points over the next 7 days using the real NBA schedule and your lineup slots, so a player only helps on days you have room to start him. A weaker player with a busier week is labeled "Short-term streamer".

**Win chance.** Football compares projected totals and assumes a typical swing of about 20 points per team. Basketball adds each team's best daily lineup for the remaining days to the current score. Both are estimates.

**Context that adjusts every suggestion.** Each of these appears as a sentence in the "why", and the ones marked with a percentage also move the number.

| Factor | Where it comes from | Effect |
|---|---|---|
| Matchup | Fantasy points each defense has allowed to each position this season, scored with your league's settings | Up to +/-6% this week |
| Defensive style | Whether a defense is a pass funnel or run funnel (tough against one, soft against the other) | Explained, not scored separately |
| Offensive and play-calling style | Each team's pass rate and plays per game, ranked across the league | Explained; already reflected in projections |
| Role | Share of backfield carries or team targets, snap share, and whether carries plus targets are rising or falling over the last three games | +/-4% for a growing or shrinking role |
| Injuries around him | Sleeper depth charts: a backup whose starter is out is the "next man up" | +10% this week |
| Quarterback situation | Compares who is expected to start at quarterback this week with who has thrown most of the team's passes this season. A backup costs his receivers 8%; a rookie or a third quarterback of the season costs them 14% and adds a "Backup QB risk" label. Running backs lose 3 to 5% | -3% to -14% |
| Teammates' absences | When one of a team's top two target-earners is out, the remaining receivers and tight ends get a bump | +5% |
| Betting lines | Expected points for each team and the point spread, when ESPN lists them. High-scoring games help everyone; big favorites run more, big underdogs throw more | Up to +/-5% for scoring, +/-3% for game script |
| Team changes | A player whose current team differs from the team he last played for is flagged as still settling in | Explained |
| Headlines | Each player's latest ESPN notes and Google News items are scanned for injury, role and trade language from the last 4 days | Changes the label to "Check news" |
| Trusted voices | Mentions by your YouTube channels | +/-6% this week, +8% / -6% rest of season, and can change the label |
| Basketball | Opponent's points allowed and pace (ESPN standings), stars out on the same team, and minutes the coach is adding or cutting | +/-3% matchup, +4% when a star teammate is out |

"Coach style" is measured, not looked up: the app does not have a database of coaches, so it uses what each team's play-calling has actually produced this season (pass rate, tempo, how the backfield and targets are split, and minutes trends in basketball).

**How trusted voices are read.** The app reads the channel's Videos page for its newest uploads, then each video's own page for the title, date and full description (including chapter lists). YouTube does not let a tool like this read what is said in a video, so to have the spoken advice counted, open the video on YouTube, choose "Show transcript", copy it, and paste it into the box on the Trusted voices tab. Your own notes work too; pasted text is kept on this computer for 10 days. The "Videos checked" table says what was read for each. It looks for your players' full names and judges the lean from nearby words such as start, add, buy, sit, sell or fade. That is keyword matching, not understanding, so sarcasm, comparisons and auto-caption misspellings can fool it. Open the linked clip before acting on a lean.

**Cross-checks and judgment.** The chips under the matchup card show which outside sources loaded:

| Source | Used for |
|---|---|
| ESPN projections and start rates | Second projection; how many managers start or roster a player |
| FantasyPros expert consensus | Weekly position ranks; rest-of-season ranks |
| Sleeper trending adds | Who other managers are picking up |

When experts disagree with the numbers, the suggestion is downgraded to "Toss-up" or "Experts disagree", and some are dropped entirely (for example a bench-only pickup when experts clearly prefer the player you would drop). If a source is unavailable the app still works and the chip shows it was skipped.

These are simple models. They do not know about weather, playoff schedules, or your league-mates' habits, and the context adjustments are deliberately small so that no single factor overrides the projections.

## Files

| File | What it is |
|---|---|
| `fantasy_gm.py` | The whole app |
| `Start Fantasy GM.command` | Double-click launcher for macOS |
| `fantasy_gm_config.json` | Created when you connect a league. Holds your username, league and ESPN sign-in |
| `fantasy_gm_tracker.json` | Created on first use. The lineups the app recommended, kept so they can be graded later |
| `.fantasy_gm_cache/` | Cached player lists, weekly stats, video details and pasted notes |

## Data sources

| Source | Notes |
|---|---|
| Sleeper API (`api.sleeper.app/v1`) | Official and public |
| Sleeper projections and stats endpoints | Not in Sleeper's official docs; may change |
| ESPN fantasy API (`lm-api-reads.fantasy.espn.com`) | Unofficial; may change |
| ESPN site API | Schedule, headlines, player notes. Unofficial |
| FantasyPros rankings pages | Read from the public web pages; may change or be blocked |
| Sleeper weekly stats and depth charts | Used for matchups, team tendencies, roles and next-man-up. Weekly stats are unofficial; finished weeks are cached on disk |
| ESPN standings | Basketball opponent defense and pace. Unofficial |
| ESPN NFL scoreboard | Remaining schedule and bye weeks. Unofficial |
| YouTube channel and video pages | Titles, dates and descriptions. Captions are usually not available |
| Google News RSS | Free, no key |

Player photos load directly from Sleeper's and ESPN's image servers.

## Privacy and security

- The server listens only on 127.0.0.1, so other devices cannot reach it.
- Your ESPN sign-in is stored in plain text in `fantasy_gm_config.json` and sent only to ESPN. Treat that file like a password. **Do not share it or commit it to GitHub**; add `fantasy_gm_config.json`, `fantasy_gm_tracker.json` and `.fantasy_gm_cache/` to `.gitignore`.
- "Find my ESPN sign-in" reads only the two ESPN cookies from your browser, only when you press the button.
- To wipe everything, use Settings > More options > Disconnect ESPN, or delete the config file and cache folder.

## Troubleshooting

| Problem | Fix |
|---|---|
| "Certificate check failed" | python.org Python on macOS: run `Install Certificates.command` in the Python folder under Applications |
| `python` or `python3` is not recognized | See "What you need installed" |
| "ESPN says this league is private" | Finish or repeat the ESPN sign-in step in Settings |
| "No ESPN sign-in was found" | Sign in at fantasy.espn.com in Chrome or Firefox first, or use the by-hand option |
| "ESPN has no basketball league..." | Paste your team link again in Settings |
| Could not start on port 8765 | It is already running in another window. Close that one, or use `--port 8766` |
| A YouTube channel shows an error | Open the channel on YouTube, copy its address, and paste it into Step 3 in Settings |
| Trusted voices says "title and description" | That is normal: YouTube does not share captions with tools. Paste the transcript on the Trusted voices tab to have it counted |
| The first load of the week is slow | It is downloading this season's weekly stats once; later loads use the saved copy |
| A source chip is greyed out | That outside source did not load. Suggestions still work without it; press Refresh later |
| A yellow note on the matchup card | The app fell back to partial data; the note says what was missing |

## Disclaimer

Fantasy GM is an independent personal project. It is not affiliated with or endorsed by Sleeper, ESPN, FantasyPros, Google, YouTube or any YouTube channel it reads. Apart from Sleeper's public API, the data sources it uses are unofficial and undocumented, so they can change or stop working at any time, and their terms of use may not permit automated access. Use it for personal, non-commercial purposes and at your own risk. Its suggestions are estimates, not guarantees.

## License

MIT. See [LICENSE](LICENSE).

