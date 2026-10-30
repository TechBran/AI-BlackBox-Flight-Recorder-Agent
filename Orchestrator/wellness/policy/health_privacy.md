# BlackBox Health: privacy policy

Version 2026-10-29

BlackBox Health helps you understand your own health and fitness data and remembers it alongside your day. It is a wellness feature. It does not diagnose, treat or monitor any condition, it is not a medical device and it is not an emergency or medical alert system.

## Who this covers

Health is set up by one person, on their own Android phone, for themselves. Each person on a BlackBox has their own separate health store. Nobody else on the box, including its owner, can read, set up or change your health data or your choices. Health is for adults only: setup asks you to confirm that the data is your own, that only you use this phone's Health Connect, and that you are an adult.

## What BlackBox reads

With your permission, the BlackBox app reads (and never writes) these kinds of data from Android Health Connect, which is where Samsung Health and other apps on your phone store what your watch, ring, cuff or scale records:

- **Sleep:** sleep sessions and their stages.
- **Heart:** heart rate (in 5-minute summaries, and 1-minute summaries during a workout), resting heart rate, heart rate variability, blood oxygen and breathing rate, when your devices record them.
- **Activity:** steps, distance and calories by the hour, and your workouts (type, time, distance and heart rate). Workout routes are never read; they are location history.
- **Blood pressure:** only if you turn it on. It is off by default, and when it is on it is answered only by AI on your own network unless you choose otherwise.

You choose each category (off, ask, or proactive) and you can change your choice at any time in Settings, Health, on your phone. Turning a category off in BlackBox or in Health Connect stops BlackBox using it on your next question.

BlackBox also notes which apps and devices wrote the data (for example "Samsung Health, a watch"), counts of records, and whether the phone could sync. It does not read anything else on your phone for this feature.

## Background reading

BlackBox reads in the background only if you grant Health Connect's background permission, which is a separate choice you make on the phone. Without it, BlackBox reads only while the app is open. A first import of your history shows a visible notification while it runs; there are no hidden reads.

## Where your data goes

- **Your BlackBox.** The phone sends your data over your private Tailscale network to your own BlackBox, into a private store for you alone (a file readable only by the BlackBox service, in a private folder). Each request is signed with a key that only your enrolled phone holds.
- **The AI that answers.** When you ask a health question, BlackBox gives the AI only the numbers that question needs. AI running on your own network, and on your phone, may answer by default. A cloud AI provider (such as Anthropic, OpenAI, Google or xAI) sees your health data only if you tick it in Settings, Health; Google also needs you to confirm your key is on a paid plan. "Only AI on my network" keeps every answer on your own hardware.
- **Who saw it.** Every time health data reaches an AI, BlackBox records when, from which screen, which provider and model, and which categories, so you can see it in "Who saw my health data".

## What never happens

- Your health numbers are never written into BlackBox's permanent conversation memory. A health conversation is saved with its numbers removed and a marker that hides it from anyone but you, and from any AI provider you did not allow. Your own words are saved as you typed them.
- Health numbers are never written to the box's logs, to the Portal's web files or to the box's archives.
- Your health data is never sold and never used for advertising, insurance, credit or employment decisions. Nothing about your health is sent to the company that makes BlackBox.
- BlackBox never answers health questions over text messages, phone calls, remote tool connections, or command-line and computer-control agents.
- An AI can never change your health settings; it can only tell you where to change them.
- BlackBox never contacts emergency services.

## Deleting your data

- **In Health Connect:** when you delete a record there, BlackBox removes its copy on the next sync. Deleting old step or heart-rate detail that was imported in bulk may not be mirrored for data older than 90 days.
- **Pause:** collection stops; BlackBox can still answer from what it already stored and says that it is paused.
- **Disconnect:** collection stops, and you choose to keep or delete what was stored. Kept history comes back if you set up Health again on the same phone; a different phone starts with a new, empty store, and Stop & erase removes both.
- **Moving to a new phone:** while your current phone is set up, it is asked to approve the move. If it cannot be reached, the move goes ahead, your stored history moves with it, and the old phone is told and stops sending.
- **Stop & erase:** from your enrolled phone, BlackBox deletes your health store, revokes your phone's key and tells the phone to forget its health settings. Your past health conversations stay in your BlackBox memory, but they contain no health numbers.
- **If your operator is removed from the box,** your health store is erased the same way.

## Backups and encryption

- There is **no backup** of your health store in this version. If the box's disk fails, the stored history is lost; Health Connect on your phone still holds your recent data.
- **In transit,** data travels over Tailscale (WireGuard encryption), and over HTTPS where the box is set up for it.
- **At rest,** the store is a private file on the box, with no encryption of its own in this version. The box checks whether its disk is encrypted (full-disk encryption) and shows the answer in Settings, Health.

## Changes to this policy

When this policy changes, its version changes, and the BlackBox app shows you the new version before it collects anything more.
