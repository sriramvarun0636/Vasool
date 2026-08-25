import re

with open('README.md', 'r') as f:
    content = f.read()

# Split by "## "
parts = re.split(r'(?m)^## ', content)

# parts[0] is the intro
# parts[1] is Exhibit D
# parts[2] is Exhibit C
# parts[3] is Exhibit B
# parts[4] is Exhibit A
# parts[5] is ☠️ Exhibit D (Supplement)
# parts[6] is 🧩 The Executable Primitive
# parts[7] is ⚡ Quickstart
# parts[8] is Exhibit E

# We want Intro, A, B, C, D, D (Supplement), Primitive, Quickstart, E

new_content = parts[0] + \
    "## " + parts[4] + \
    "## " + parts[3] + \
    "## " + parts[2] + \
    "## " + parts[1] + \
    "## " + parts[5] + \
    "## " + parts[6] + \
    "## " + parts[7] + \
    "## " + parts[8]

with open('README.md', 'w') as f:
    f.write(new_content)

print("Reordered successfully!")
