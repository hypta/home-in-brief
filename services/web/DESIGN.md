# Design reference: the page

The page reads like a property record that someone has gone through with a highlighter and a pen. Four voices,
each with one job:

| Voice | Looks like | Says |
|---|---|---|
| The page | Figtree | Records, figures, notes, labels, controls |
| Checked sentences | Highlighter over the same type | A sentence report-api returned with sources: the summary and checked answers. Nothing else is highlighted |
| The record | DM Mono, 0.8125 rem, one weight | When, from where and how something was checked: dates, "Checked against 3 sources", source details. Also numbers copied from a record, such as a title number (0.9375 rem) |
| Our notes | Caveat, in pen blue | A few words of guidance from us, such as "start here". Never a fact, a figure or a caveat |

## Colour (variables in `app/static/styles.css`; the theme follows the system setting until the reader switches it)

| Variable | Light | Dark | Use |
|---|---|---|---|
| `--paper` | `#fff` | `#171c26` | The answer sheet, inputs |
| `--desk` | `#eef1f5` | `#0f131b` | Page, the records below the sheet |
| `--ink` | `#1a2233` | `#e9ecf2` | Text; the reader's own question (text in `--paper`) |
| `--pencil` | `#535e6e` | `#a9b2c0` | Secondary text, on `--paper` or `--desk` only |
| `--line` / `--edge` | `#d2d8e0` / `#6a7585` | `#2c3442` / `#6f7b8d` | Hairlines / control borders and dashed "not known" boxes |
| `--pen` / `--on-pen` | `#1b45b4` / `#fff` | `#9db9ff` / `#0f131b` | Links, buttons, hand-drawn marks, focus ring |
| `--hl` | `#ffe45c` | `#5a5000` | Highlighter |
| `--logo` | `#eaa800` | `#ffe45c` | The block under the roof beside the page name |
| `--heed` / `--heed-wash` | `#8a3b00` / `#ffe3c7` | `#ffc58a` / `#3d2a14` | Mapped here, a condition, a part that failed |
| `--quiet` | `#e4e8ee` | `#222a36` | Neutral wash: a tag, a hovered option |

Text pairs are 5.8:1 or better; control borders and marks (`--edge`, `--pen`, `--heed`) are about 4:1 or better on
`--paper` and `--desk` in both themes (calculated). Hairlines in `--line` are decoration and are not counted. No green
and no red: beside a hazard they read as a verdict. The map keeps its own colours; the landslide and liquefaction
classes are the council's own names and grading, not a rating by this tool.

## Type, space, shape

- Main sizes in rem: 0.8125 record; 0.875 secondary; 1 body; 1.125 a checked sentence (line height 1.8, so the
  highlighter bands do not touch); 1.1875 headings; 1.5 the address, 1.875 from 720 px. Sentence case everywhere.
- Chinese, Japanese and Korean text falls back to the system font. Caveat is never applied to text from an API.
- Sections are 48 px apart, sentences 26 px, notes 14 px above and below, record rows 6 px (10 px on a
  narrow screen). Controls are at least 44 px high, inputs 48.
- Drawn by hand: the address box, the question box and the "could not be found" box (`--box`), section numbers
  and step marks (`--ring`), tick boxes, one underline under the address. Everything else has plain, evenly rounded corners.
  No shadows, no gradients.

## Parts

- **Entry.** The address box is the first control. Under it, the details the figures were calculated with, one line
  each, and "Change". The form is open until the first results are on the page. While the form is open, choosing an
  address asks for nothing: the lookup starts with the button, which before the first results stays at the bottom of
  the screen, on a band of the page's colour; beside it on a wide screen, in our hand, "it's free!". With the form folded away, choosing another address starts the next lookup at once.
- **Steps.** "Records", then "Summary and check": how far the page has got, on one line and in the page's own
  type. The step under way says what is being done in the place of its name and with the name's own words ("Reading
  the records…", "Writing and checking the summary…"), and its mark pulses. A step ends when the page sees its request
  finish, never on a timer. A failed step is dashed. Once both are done the line goes.
- **Checked sentence.** Highlighted, then one control, "Checked against N sources", that opens the source cards
  in place. A sentence with no source (a fixed sentence from the program) is plain.
- **Source control.** Every other sourced line ends with "Source" or "N sources", and the control stays where it is
  when it is opened. A card is the source's name as
  the link and nothing else. The words quoted from it, the date of a hazard layer's data, its credit line, licence and
  dates are in the full list of the last section; the footer credits LINZ and Auckland Council on every page.
- **Theme switch.** A plain line icon of the theme it switches to: a moon on a light page, a sun on a dark one.
- **Next steps.** One tick list on the sheet, open under a line that says how many there are and how many are
  ticked; the line folds it away. A next step is said once on the page: not under a note when the list has it, and under the first note only when two notes share it.
- **Map.** Its credits stay on its lower edge, always in view, in small type.
- **Hazard layer.** The mark the layer has on the map, its name, what was found, a bar as long as the share of the
  parcel it covers (a measure of area, not of risk), its source. Layers are grouped by what was found, and each group says its caveat once.
- **Lead figure.** The figure a section opens with, large, with a few words saying what it is; said once.
- **Fold.** A row that names what is inside and how many: "First Home Loan rules (8 notes)". Notes that are the
  same for every home, long rule lists and the rest of the council record are folded; a note about this property is not. The note on flood cover stays in view where a flood layer is mapped on the parcel.
- **Could not be found.** One dashed box that names what the records lack in one line, with the reasons behind it.
  A layer that could not be checked keeps its caveat sentence in view.
- **Key figures.** Under the summary sentences, a short list the program writes from the records and the calculation:
  the council valuation (not a market value), the monthly repayments and, when the loan is above it, the high-DTI
  threshold for this income. Plain text, never highlighted, each with its
  sources.
- **Section.** A drawn number, the title, one line of digest from API values (shown while the section is closed), a chevron. Closed by default; the
  hazards section is open when a flood layer is mapped on the parcel. Four sections: hazards, this home, money, sources.
  A note about the build era, the cladding or the title sits under the record it explains in "This home"; a note that
  says what a term means (CV, LVR, DTI) sits under the row that uses it. Money leads with at most two repayments (the
  visitor's own rate, or else the banks' published rates, and the First Home Loan); the details of each are folded.
- **Hazards order.** One line counts how the layers fell; then could not be checked, mapped, region-wide classes; the
  layers not shown are named on one line with their caveat and one source control.
- **Arrival.** After a lookup the sheet shows the steps alone; the sections, the map and the questions come in one piece
  when the summary is written or has failed (at most 20 s). A page restored in the tab, Update and a search that fills
  the form do not hide the page again.
- **Questions.** One panel: a bar at the bottom of a narrow screen that opens upward (its handle shows once there is an
  answer, or while the panel is open), a column beside the results
  from 1000 px. Until something has been asked it offers three questions to start with, and a fourth about this home's
  title type when the records give it; choosing one asks it. The box says "Ask about this home"; over it, in our hand until the first question is asked, "your own language is fine" and an arrow.
- **Footer.** Two fixed lines, the same with or without results: what the tool is not, and the LINZ and Auckland
  Council credits with a link to the licence.
- **Answer from a web search.** Plain text, no highlighter and no box: the text, its sources and the search
  suggestions as returned and in that order, the sources on one small line. Under them, in the page's own
  type, one small paragraph: "Found by Gemini with Google Search. Not checked by this tool." and the links report-api
  names for checking it.
- **Fixed reply.** Plain text, no highlighter, no source. The conversation has two looks only: a checked sentence
  is highlighted, everything else is plain.
- **First summary.** The explanation of how the summary is made, and our margin notes, are open for the first
  summary in a tab and folded or hidden after it. A caveat is never treated this way.
- **Motion.** The step in progress pulses; while an answer is on its way three dots pulse and a few words take
  turns (they say that work is going on, never which part of it); a checked answer's words appear in order (a web-search answer is shown
  at once). Both are off under `prefers-reduced-motion`.

## Do

- Keep the caveat with every "not mapped", "not found" or "could not be checked", in full and in view.
- Say a fact once at the top level; put detail behind a control that names what is inside and how much.
- Write facts only from API values and knowledge-base items. Labels and help text are ours.
- Make every control reachable by keyboard and by touch. Nothing opens on hover only.

## Do not

- Do not highlight for emphasis, status or anything the program wrote without a source.
- Do not put a fact, a figure or a caveat in Caveat or in the record voice.
- Do not use verdict words (safe, risky, recommended), scores, or a meter that reads as a risk level.
- Do not add: capitals-only labels, small labels above headings, icons above headings, emoji, nested panels,
  status tags that repeat the sentence beside them, an arrow after link text, numbers on things that are not a sequence.
