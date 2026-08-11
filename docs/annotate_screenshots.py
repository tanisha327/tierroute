"""Draw numbered markers on the UI screenshots used by README.md.

Each marker is a red box around one thing on screen plus a numbered circle.
A short note for every number is printed underneath the picture, so each
picture explains itself without the README next to it.

Run it again after re-taking a screenshot:

    python docs/annotate_screenshots.py <folder-with-raw-screenshots>

Raw files keep their original names; the annotated copies are written to
docs/images/ with the names README.md expects.
"""

import os
import sys

from PIL import Image, ImageDraw, ImageFont

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "images")

RED = (225, 29, 72)
WHITE = (255, 255, 255)
INK = (26, 26, 26)
RULE = (214, 214, 214)

# name of the annotated file -> (raw screenshot file, [markers])
# a marker is (box, corner, note):
#   box    = (left, top, right, bottom) in pixels of the raw screenshot
#   corner = where the number sits: "tl" top-left, "tr" top-right,
#            "br" bottom-right, "ml" out in the left margin. Pick whichever
#            spot has empty space next to it.
#   note   = the line printed under the picture. Keep it to one short sentence.
SHOTS = {
    "01-open-the-app.png": (
        "{7100B1B5-88D5-43A5-9873-DD773FF349C7}.png",
        "Step 1. What you see when it opens",
        [
            (
                (80, 140, 1520, 310),
                "tl",
                "GitLab box. Only needed if the job has to talk to GitLab. Paste your "
                "token, press Start broker. Skip it for local jobs.",
            ),
            (
                (80, 336, 1520, 570),
                "tl",
                "The job. Say what you want done, in your own words, and which folder "
                "to do it in.",
            ),
            (
                (660, 500, 948, 552),
                "tr",
                "Ticked: it may change your files and run commands. Not ticked: it only "
                "reads and reports back.",
            ),
            (
                (80, 594, 1520, 812),
                "tl",
                "Four folding panels of extra settings. They already have sensible "
                "values, so you can leave them alone.",
            ),
            ((98, 870, 192, 930), "tl", "Press Run to start."),
            (
                (80, 1008, 1520, 1552),
                "tr",
                "The work appears here, line by line, while it happens.",
            ),
        ],
    ),
    "02-ready-to-run.png": (
        "{08F79D32-0FE2-4B2B-90CC-2260B3AC03B6}.png",
        "Step 2. Filled in and ready to go",
        [
            (
                (170, 168, 470, 206),
                "tr",
                "Green dot: the token was accepted. It is kept in this program's memory "
                "only, and the AI never sees it.",
            ),
            (
                (176, 206, 836, 266),
                "tl",
                "Paste the token here, then press Start broker. Once per session, "
                "nothing is saved to disk.",
            ),
            (
                (178, 362, 1582, 478),
                "tl",
                "What you want done. One or two sentences is enough.",
            ),
            ((250, 494, 732, 542), "br", "The folder it works in."),
            (
                (748, 498, 1018, 542),
                "tr",
                "Ticked, so it is allowed to edit files and run tests.",
            ),
            (
                (178, 864, 274, 922),
                "tl",
                "Press Run. Everything after this is automatic.",
            ),
        ],
    ),
    "03-pick-the-models.png": (
        "{9DA172F7-0BF9-4036-9E81-186A53719CC3}.png",
        "Step 3. Choosing which AI does the work",
        [
            (
                (160, 604, 312, 656),
                "tr",
                "Baseline: the model you would otherwise have used for everything. "
                "Nothing runs on it. It is only used to work out what you saved.",
            ),
            (
                (160, 658, 312, 782),
                "br",
                "Biggest and priciest at the top, smallest and cheapest at the bottom.",
            ),
            (
                (396, 604, 542, 656),
                "tr",
                "Planner: reads your request once and splits it into steps. A mid range "
                "choice is plenty.",
            ),
            (
                (624, 604, 770, 656),
                "tr",
                "Review: checks the finished work. Also fine on a mid range choice.",
            ),
        ],
    ),
    "04-set-spending-limits.png": (
        "{A0BD655B-3711-472F-9F4D-6CCCA75EBCD3}.png",
        "Step 4. Putting a limit on what it can spend",
        [
            (
                (190, 660, 326, 710),
                "tr",
                "Money limit. The run stops the moment spending reaches this. The "
                "important one.",
            ),
            (
                (430, 660, 568, 710),
                "tr",
                "Step limit. The run also stops after this many steps.",
            ),
            (
                (806, 660, 944, 710),
                "tr",
                "Past this amount it stops using the expensive model and finishes on "
                "cheap ones.",
            ),
            (
                (1092, 660, 1226, 710),
                "tr",
                "If it repeats the same action this many times in a row it is stuck, so "
                "the run is stopped.",
            ),
        ],
    ),
    "05-check-the-work.png": (
        "{3E578255-BD25-47AC-8F1F-BCAB0694AD69}.png",
        "Step 5. Having the work checked",
        [
            (
                (50, 714, 210, 750),
                "ml",
                "Ticked: when the job is done, a second AI reads the result and decides "
                "whether it did what you asked. Leave this on.",
            ),
            (
                (318, 708, 456, 752),
                "tr",
                "If the check finds problems it fixes them and checks again, up to this "
                "many times.",
            ),
            (
                (606, 708, 746, 752),
                "tr",
                "How sure the checker has to be before the job counts as finished. 0.7 "
                "means fairly sure. Higher is fussier.",
            ),
        ],
    ),
    "06-skip-the-check.png": (
        "{9B4FAA53-CA5C-4B55-A456-D267CB198AD6}.png",
        "Step 6. Skipping the check",
        [
            (
                (74, 722, 236, 762),
                "ml",
                "Not ticked: the job ends as soon as the last step is done. Quicker and "
                "cheaper, but nobody double checks the result.",
            ),
        ],
    ),
    "07-while-it-runs.png": (
        "image.png",
        "Step 7. While it runs",
        [
            (
                (336, 116, 1420, 176),
                "tr",
                "Your settings stay on screen, so you can see what this run was given.",
            ),
            (
                (332, 596, 600, 668),
                "tl",
                "Run is greyed out while it works. Press Stop to end the run at any "
                "point.",
            ),
            (
                (330, 790, 1100, 872),
                "tr",
                "Live progress. round 1/3 is the check and fix round it is on, and the "
                "name after planning on is the AI doing that step.",
            ),
        ],
    ),
    "08-reading-the-output.png": (
        "run-log.png",
        "Step 8. Reading the output of a finished run",
        [
            (
                (332, 795, 1050, 865),
                "ml",
                "Round 1 of up to 3. A round is: make a plan, do the steps, check the "
                "result. Planning runs on a mid tier model.",
            ),
            (
                (332, 868, 1810, 938),
                "ml",
                "The plan came back with 2 steps and cost 9 cents. Each step line shows "
                "the tier in brackets, an arrow to the AI that ran it, and the money "
                "spent up to that point.",
            ),
            (
                (332, 941, 1900, 1078),
                "ml",
                "Both steps ran on the cheap model. done shows what that step cost, and "
                "session spent is the running total for the whole run.",
            ),
            (
                (332, 1079, 1480, 1149),
                "ml",
                "The checker found 3 problems and was only 0.72 sure, so the work was "
                "not accepted and it goes back to planning for round 2.",
            ),
            (
                (332, 1322, 1720, 1359),
                "ml",
                "Spending passed the reasoning limit, so the expensive model is switched "
                "off and the rest of the run finishes on the cheap tier.",
            ),
        ],
    ),
}


def _font(size, bold=False):
    names = (
        ("arialbd.ttf", "segoeuib.ttf", "DejaVuSans-Bold.ttf")
        if bold
        else ("arial.ttf", "segoeui.ttf", "DejaVuSans.ttf")
    )
    for folder in (r"C:\Windows\Fonts", "/usr/share/fonts/truetype/dejavu"):
        for name in names:
            path = os.path.join(folder, name)
            if os.path.exists(path):
                return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _wrap(draw, text, font, limit):
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if line and draw.textlength(trial, font=font) > limit:
            lines.append(line)
            line = word
        else:
            line = trial
    if line:
        lines.append(line)
    return lines


def _mark(draw, markers, scale):
    """Draw the red box and the numbered circle for every marker."""
    box_width = max(3, round(4 * scale))
    radius = max(14, round(22 * scale))
    font = _font(max(16, round(26 * scale)), bold=True)

    for number, (box, corner, _note) in enumerate(markers, start=1):
        draw.rectangle(box, outline=RED, width=box_width)
        if corner == "ml":
            cx = max(radius + 4, box[0] - radius - 10)
            cy = (box[1] + box[3]) // 2
        else:
            cx = box[0] if corner == "tl" else box[2]
            cy = box[3] if corner == "br" else box[1]
        draw.ellipse(
            (cx - radius, cy - radius, cx + radius, cy + radius),
            fill=RED,
            outline=WHITE,
            width=max(2, round(3 * scale)),
        )
        label = str(number)
        left, top, right, bottom = draw.textbbox((0, 0), label, font=font)
        draw.text(
            (cx - (right - left) / 2 - left, cy - (bottom - top) / 2 - top),
            label,
            fill=WHITE,
            font=font,
        )


def _caption_strip(img, title, markers, scale):
    """Return a white strip that explains every number, sized to fit the text."""
    pad = round(34 * scale)
    radius = max(13, round(19 * scale))
    gap = round(20 * scale)
    text_font = _font(max(15, round(25 * scale)))
    title_font = _font(max(17, round(29 * scale)), bold=True)
    line_step = round(34 * scale)

    measure = ImageDraw.Draw(img)
    text_left = pad + 2 * radius + gap
    text_limit = img.width - text_left - pad
    blocks = [
        _wrap(measure, note, text_font, text_limit) for (_box, _corner, note) in markers
    ]

    height = pad + line_step + round(14 * scale)
    for lines in blocks:
        height += max(len(lines) * line_step, 2 * radius) + round(16 * scale)
    height += pad - round(16 * scale)

    strip = Image.new("RGB", (img.width, height), WHITE)
    draw = ImageDraw.Draw(strip)
    draw.line((0, 0, img.width, 0), fill=RULE, width=max(2, round(2 * scale)))
    draw.text((pad, pad), title, fill=INK, font=title_font)

    y = pad + line_step + round(14 * scale)
    for number, lines in enumerate(blocks, start=1):
        cx, cy = pad + radius, y + radius
        draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=RED)
        label = str(number)
        left, top, right, bottom = draw.textbbox((0, 0), label, font=title_font)
        draw.text(
            (cx - (right - left) / 2 - left, cy - (bottom - top) / 2 - top),
            label,
            fill=WHITE,
            font=title_font,
        )
        for row, line in enumerate(lines):
            draw.text((text_left, y + row * line_step), line, fill=INK, font=text_font)
        y += max(len(lines) * line_step, 2 * radius) + round(16 * scale)
    return strip


def annotate(src, title, markers, dest):
    shot = Image.open(src).convert("RGB")
    scale = shot.width / 1600.0
    _mark(ImageDraw.Draw(shot), markers, scale)

    strip = _caption_strip(shot, title, markers, scale)
    out = Image.new("RGB", (shot.width, shot.height + strip.height), WHITE)
    out.paste(shot, (0, 0))
    out.paste(strip, (0, shot.height))
    out.save(dest)
    print(f"wrote {dest}")


def main():
    raw_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    os.makedirs(OUT_DIR, exist_ok=True)
    for name, (raw, title, markers) in SHOTS.items():
        src = os.path.join(raw_dir, raw)
        if not os.path.exists(src):
            print(f"skipped {name}: no {src}")
            continue
        annotate(src, title, markers, os.path.join(OUT_DIR, name))


if __name__ == "__main__":
    main()
