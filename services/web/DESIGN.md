# Design reference: the report page

The page reads like a property record that someone has gone through with a highlighter and a pen. Four voices,
each with one job:

| Voice | Looks like | Says |
|---|---|---|
| The report | Figtree | Records, figures, notes, labels, controls |
| Checked sentences | Highlighter over the same type | A sentence report-api returned with sources: the summary and checked answers. Nothing else is highlighted |
| The record | DM Mono, 0.8125 rem, one weight | When, from where and how something was checked: dates, "Checked against 3 sources", source details, the model's name. Also numbers copied from a record, such as a title number |
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
| `--heed` / `--heed-wash` | `#8a3b00` / `#ffe3c7` | `#ffc58a` / `#3d2a14` | Mapped here, a condition, a part that failed |
| `--quiet` | `#e4e8ee` | `#222a36` | Neutral wash: not found, an answer from a web search |

Text pairs are 5.8:1 or better and borders and marks 4:1 or better in both themes (calculated). No green and no
red: beside a hazard they read as a verdict. The map keeps its own colours.

## Type, space, shape

- Sizes in rem: 0.8125 record; 0.875 secondary; 1 body; 1.125 a checked sentence (line height 1.8, so the
  highlighter bands do not touch); 1.1875 headings; 1.5 the address, 1.875 from 720 px. Sentence case everywhere.
- Chinese, Japanese and Korean text falls back to the system font. Caveat is never applied to text from an API.
- Sections are 48 px apart, sentences 26 px, rows 14 px above and below. Controls are at least 44 px high, inputs 48.
- Drawn by hand: the address box, the question box and the "could not be found" box (`--box`), section numbers
  and step marks (`--ring`), tick boxes, one underline under the address. Everything else is square and plain.
  No shadows, no gradients.

## Parts

- **Entry.** The address box is the first control. Under it, the details the report was calculated with, one line
  each, and "Change". The form is open until the first report is on the page.
- **Steps.** Facts, Wording, Source check: how the summary is made and how far it has got. A failed step is dashed.
- **Checked sentence.** Highlighted, then one control, "Checked against N sources", that opens the source cards
  in place. A sentence with no source (a fixed sentence from the program) is plain.
- **Source control.** Every other sourced line ends with "Source" or "N sources". A card is the source's name as
  the link, then quote, dates, credit line and licence in the record voice.
- **Next steps.** One tick list on the sheet. A note repeats its next step only when the list does not have it.
- **Hazard layer.** The mark the layer has on the map, its name, what was found, a bar as long as the share of the
  parcel it covers, its source. Layers are grouped by what was found, and each group says its caveat once.
- **Lead figure.** The figure a section opens with, large, with a few words saying what it is; said once.
- **Fold.** A row that names what is inside and how many: "First Home Loan rules (8 notes)". Notes that are the
  same for every home, long rule lists and the rest of the council record are folded; a note about this property is not.
- **Could not be found.** One dashed box. A layer that could not be checked keeps its caveat sentence.
- **Section.** A drawn number, the title, one line of digest from API values, a chevron. Closed by default; the
  hazards section is open when a flood layer is mapped on the parcel.
- **Answer from a web search.** Neutral wash, the label "Found by Gemini with Google Search. Not checked against
  this report's sources." in bold, no highlighter, text and search suggestions as returned.
- **Fixed reply.** Dashed box, no highlighter, no source.
- **Motion.** The step in progress pulses; an answer's words appear in order. Both are off under
  `prefers-reduced-motion`.

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
