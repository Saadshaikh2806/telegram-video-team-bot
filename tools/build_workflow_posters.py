"""Create the printable team guide. Run with ReportLab installed."""
from pathlib import Path
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.colors import HexColor
from reportlab.lib.utils import simpleSplit

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'output' / 'pdf'
OUT.mkdir(parents=True, exist_ok=True)
pdfmetrics.registerFont(TTFont('Team', 'C:/Windows/Fonts/segoeui.ttf'))
pdfmetrics.registerFont(TTFont('TeamBold', 'C:/Windows/Fonts/segoeuib.ttf'))
W, H = 720, 900
NAVY, INK, MUTED, PAPER = '#12263A', '#172D40', '#52687B', '#F4F7FA'
pages = [
    ('01', 'THE BIG PICTURE', 'One video.\nFour simple steps.', '#087F8C',
     'Two Telegram groups. One clear path from footage to finished edit.',
     [('UPLOAD', 'Send footage + instructions', 'Post in the Uploaders group. A source-folder link works too.'),
      ('ASSIGN', 'The bot chooses an editor', 'Available editors share work by allocated effort. Admins and the group owner are excluded.'),
      ('EDIT', 'Start, edit, then reply', 'The assigned editor taps Start editing and sends the finished file through Submit edit.'),
      ('REVIEW', 'Approve or request changes', 'An admin reviews the result. Approved edits return to the Uploaders group.')],
     'YOUR HOME BASE', 'Open the pinned Team controls message in the Editors group. Commands remain optional shortcuts.'),
    ('02', 'FOR UPLOADERS', 'Send the footage.\nWe handle the queue.', '#3367C1',
     'Use the Uploaders group for new videos and finished results.',
     [('1', 'Post one video per job', 'Attach the video and describe the edit in its caption. Avoid albums for separate jobs.'),
      ('2', 'Working with a folder?', 'Paste a shareable source link with instructions. Make sure the editor can open it.'),
      ('3', 'Watch for assignment updates', 'The bot tells you who has the video and when it is due. If everyone is busy, the video waits.'),
      ('4', 'Receive the approved edit', 'The finished submission returns here after approval. No need to chase editors individually.')],
     'A GOOD BRIEF', 'Example: Add English captions, remove long pauses, and export vertically. Include any reference or brand instructions.'),
    ('03', 'FOR EDITORS', 'Tap. Edit.\nSend it back.', '#167D65',
     'Join the Editors group. Existing members can say Hi once to register.',
     [('1', 'Open My tasks', 'Find your videos through Team controls. Each status card shows the editor, due time and next action.'),
      ('2', 'Tap Start editing', 'The deadline starts when the assignment is delivered, not when you tap Start editing.'),
      ('3', 'Tap Submit edit', 'Reply to the bot question with your video, document or download link. You can also reply to the original assignment.'),
      ('4', 'Stuck? Tap Need help', 'Reply with what is blocking you. Admins are notified; your clock continues unless an admin extends it.')],
     'AFTER YOU SUBMIT', 'Deadline alerts stop while an admin reviews. If changes are requested, revise and submit again with the fresh deadline.'),
    ('04', 'FOR ADMINS', 'Manage the work.\nUse the buttons.', '#8653A6',
     'Only configured bot admins can use management actions.',
     [('REVIEW', 'Approve / Request changes', 'Open Waiting for review. Approve the finished edit, or tap Request changes and reply with feedback.'),
      ('REASSIGN', 'Change editor', 'Reply with the reason. The same video returns to the queue for a different editor, with a fresh deadline on delivery.'),
      ('ADJUST', 'More time / Cancel video', 'More time asks for extra hours and a reason. Cancel video asks for a reason before closing the job.'),
      ('TEAM', 'Editor availability', 'Choose Pause or Resume beside a name. Pausing stops new assignments; existing deadlines continue.')],
     'CHANGED YOUR MIND?', 'Reply Never mind to stop a guided action. Questions expire after one hour or when the video changes. Open the latest card to try again.'),
    ('05', 'REMINDERS & REPORTS', 'Know what is due.\nSee what gets done.', '#BB632B',
     'Automatic reminders support the team without daily status chasing.',
     [('24h', 'Default delivery window', 'Each new delivered assignment gets 24 hours by default. An admin can grant more time.'),
      ('6h / 2h', 'Due-soon reminders', 'The editor is reminded before the deadline. At the deadline, an unsubmitted video is escalated to admins.'),
      ('2 JOBS', 'Default active-job limit', 'Each editor can hold two active jobs by default. Submissions awaiting review still occupy a slot.'),
      ('WEEKLY', 'Review progress together', 'Weekly charts cover workload, completion, timeliness and first-pass approval. Admins can also tap Weekly report.')],
     'FAIR ASSIGNMENT', 'Among available editors with capacity, the bot picks the lowest allocated effort. Paused editors and admins receive no new work. Defaults can be configured.')
]

c = canvas.Canvas(str(OUT / 'video-team-workflow-posters.pdf'), pagesize=(W, H))
c.setTitle('Video Team | Button-driven Workflow Posters')
c.setAuthor('Video Team')

def text(x, y, value, font='Team', size=15, color=INK, width=580, leading=None):
    c.setFillColor(HexColor(color)); c.setFont(font, size)
    for line in simpleSplit(value, font, size, width):
        c.drawString(x, y, line)
        y -= leading or size * 1.38
    return y

for number, eyebrow, title, accent, subtitle, steps, note_title, note in pages:
    c.setFillColor(HexColor(PAPER)); c.rect(0, 0, W, H, fill=1, stroke=0)
    c.setFillColor(HexColor(NAVY)); c.rect(0, 627, W, 273, fill=1, stroke=0)
    c.setFillColor(HexColor(accent)); c.rect(0, 627, 13, 273, fill=1, stroke=0)
    text(46, 857, 'VIDEO TEAM  /  ' + eyebrow, 'TeamBold', 13, '#88DAD5')
    y = 798
    for line in title.split('\n'):
        text(44, y, line, 'TeamBold', 43, '#FFFFFF', 638)
        y -= 53
    text(46, 659, subtitle, size=14, color='#D8E5EF', width=626, leading=19)
    y = 581
    for badge, heading, body in steps:
        c.setFillColor(HexColor(accent)); c.roundRect(45, y - 24, 86, 34, 8, fill=1, stroke=0)
        c.setFillColor(HexColor('#FFFFFF')); c.setFont('TeamBold', 11)
        c.drawCentredString(88, y - 12, badge)
        text(150, y, heading, 'TeamBold', 20, width=522)
        end = text(150, y - 30, body, size=15, color=MUTED, width=510, leading=21)
        assert end > y - 101, (number, heading, end)
        y -= 112
    c.setFillColor(HexColor('#E2EBF1')); c.roundRect(45, 55, 630, 104, 12, fill=1, stroke=0)
    text(63, 133, note_title, 'TeamBold', 12, accent)
    end = text(63, 109, note, size=13, width=593, leading=18)
    assert end >= 49, (number, end)
    text(46, 25, 'TEAM QUICK GUIDE  |  Keep this near your Telegram workspace', size=10, color=MUTED)
    text(630, 25, number + ' / 05', 'TeamBold', 10, accent, 70)
    c.showPage()
c.save()
print(OUT / 'video-team-workflow-posters.pdf')
