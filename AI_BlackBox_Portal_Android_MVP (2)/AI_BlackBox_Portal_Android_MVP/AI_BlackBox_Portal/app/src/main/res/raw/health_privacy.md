# BlackBox Health: privacy policy

Version 2026-10-29

BlackBox Health helps you understand your own wearable data and remembers it alongside your day. It is a wellness feature. It is not a medical device, it does not diagnose anything, and it is not an emergency or medical alert system.

## What BlackBox reads

With your permission, the BlackBox app on your phone reads, read-only, what your watch, ring, cuff or scale has written into Android Health Connect, for the kinds of data you turn on:

- **Sleep:** sleep sessions and their stages.
- **Heart:** heart rate, resting heart rate, heart rate variability, blood oxygen and breathing rate.
- **Activity:** steps, distance, calories and workouts. Workout routes are never read (they are location history).
- **Blood pressure:** readings, only if you turn it on. It is answered by AI on your own network by default.

Each kind is its own Health Connect permission, and you can turn any of them off at any time in Health Connect or in BlackBox, Settings, Health. Reading data older than 30 days and reading while the app is closed are separate permissions you choose on their own. BlackBox never writes to Health Connect or Samsung Health.

## Where it goes

- The phone sends what it reads only to **your own BlackBox box**, over your private Tailscale network, signed with a key only this phone and your box hold.
- On the box it is kept in a private store for **you alone** (one store per person). No other person on the box can see it, including the box owner.
- An AI model sees your health data only when you ask on your enrolled phone (or a browser you approved from it), and only an AI you allowed. **AI on your own network is allowed by default. Every cloud AI provider needs your explicit tick**, and Google needs a paid key.
- Blood pressure, and anything you mark "local AI only", never goes to a cloud AI.
- Every time an AI is given your health data, the box records when, which AI and which kinds of data. You can see this list in Settings, Health, "Who saw my health data".

## What never happens

- Your health data is never sold, and never used for advertising, insurance, credit or employment decisions.
- Nothing about your health is sent to the BlackBox company.
- Health numbers are never written into the BlackBox conversation memory. Conversations about your health are remembered with their numbers removed, and they are hidden from everyone else on the box.
- Health is never answered over text messages (SMS), phone calls, remote tools or automated agents.
- BlackBox never tells you that you have a disease or condition, never calls a value abnormal or dangerous, and never advises starting, stopping or changing a medication or treatment.
- BlackBox never contacts emergency services.

## How long it is kept

Your health detail is kept on your box until you erase it. Deleting a record in Health Connect or Samsung Health removes it from BlackBox too, for sleep sessions, workouts and readings of any age, and for daily totals of the last 30 days (older daily totals are reconciled monthly for the last 90 days). There is no backup of the health store in this version.

## Your controls

In BlackBox, Settings, Health on your phone you can:

- turn each kind of data Off, Ask or Proactive;
- choose which AI may see it, or "Only AI on my network";
- **Pause** collection, or **Disconnect** and choose to keep or delete what is stored;
- **Stop and erase**, which deletes your health store and this phone's key. Your past conversations stay in your BlackBox memory, but they contain no health numbers.

## On this phone

The phone keeps its key, its sync bookkeeping and data waiting to be sent in app storage that is excluded from Android backup and device transfer. Disconnecting or erasing clears it.

## Questions

Ask whoever runs your BlackBox box. The same policy is served by your box at `/wellness/privacy-policy`.
