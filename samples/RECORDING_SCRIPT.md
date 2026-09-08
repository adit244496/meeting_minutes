# Benchmark recording script

Read this aloud with two colleagues, on the microphone and in the room you will
actually use. Five minutes of recording. This is the real benchmark — it is worth
far more than any file that could be handed to you, because it captures your
acoustics, your accents and your way of mixing languages.

## How to record it

- **Three speakers.** Diarization has nothing to do with one voice.
- **Use the real mic in the real room.** A phone held close gives flattering
  results you will not reproduce in a meeting hall.
- Speak at a normal meeting pace. Do not enunciate carefully — the point is to
  test realistic speech.
- Where the script says *(overlap)*, genuinely talk over each other.
- Save as `samples/mixed_meeting.m4a` (any format ffmpeg reads is fine).

Then:

```bash
docker compose run --rm api python scripts/spike.py /samples/mixed_meeting.m4a
```

## The script

Lines are labelled with the language pattern each one is testing. Say the
bracketed language naturally — do not translate, do not switch to English.

---

**PRIYA** *(pure English — control)*
> Right, let's start. We're here to review the payment gateway integration and
> decide whether we can ship on the twenty-third.

**RAHUL** *(Hindi matrix, English technical terms)*
> हाँ, तो deployment का status ये है कि staging पर सब कुछ काम कर रहा है, but
> production database migration अभी pending है।

**ANANYA** *(Bengali matrix, English technical terms)*
> আমাদের দিক থেকে API integration টা complete, কিন্তু load testing এখনো বাকি
> আছে। আমি কালকে সেটা finish করে দেব।

**PRIYA** *(English with one Hindi phrase)*
> Okay so the blocker is testing. Rahul, कितना time लगेगा for the migration?

**RAHUL** *(Hindi with numbers and dates — tests numeral handling)*
> लगभग दो दिन। अगर हम बुधवार को शुरू करें तो शुक्रवार तक हो जाएगा। तीन hundred
> gigabyte का data है।

**ANANYA** *(Bengali, rapid mid-sentence switching)*
> কিন্তু Rahul, তুমি যদি Friday তে finish করো, তাহলে আমার regression suite
> চালানোর জন্য শুধু একটা weekend থাকবে। That's not enough, honestly.

**PRIYA + RAHUL** *(overlap — say these simultaneously)*
> PRIYA: So we're saying the twenty-third is not realistic —
> RAHUL: नहीं नहीं, हम कर सकते हैं अगर —

**PRIYA** *(English, decision — tests whether minutes extract this)*
> Let's move the launch to the thirtieth. Rahul owns the migration, Ananya owns
> load testing and regression. We review again on Monday.

**RAHUL** *(Hindi, agreement with English proper nouns)*
> ठीक है। मैं Jenkins pipeline भी update कर दूँगा और Slack पर team को बता दूँगा।

**ANANYA** *(Bengali, question left open — tests open_questions extraction)*
> একটা প্রশ্ন আছে — Razorpay এর sandbox credentials কে renew করবে? আমার কাছে
> access নেই।

**PRIYA** *(English, closing)*
> Good question, nobody owns that yet. I'll find out and come back to you.
> Thanks everyone.

---

## What to check in the output

Score each provider on these. They are ordered by how much they matter.

### 1. Script fidelity — the main event

Look at the **code-switching report** the spike prints.

- English technical terms inside Hindi/Bengali sentences should stay in **Latin
  script**: `deployment`, `staging`, `API`, `load testing`, `Jenkins`, `Slack`,
  `Razorpay`.
- If they come back as `डिप्लॉयमेंट`, `স্টেজিং` etc., the provider is
  **transliterating** — the transcript is readable but unsearchable.
- A **high mixed-share** (`deva+latn`, `beng+latn` segments) is good. A low one
  on this script means transliteration, not clean output.

### 2. Speaker separation

Three speakers should come back as three labels, and the speaking-time split
should look roughly right. Check the overlap line — some loss there is expected
and normal.

### 3. Numbers and dates

`twenty-third`, `thirtieth`, `दो दिन`, `तीन hundred gigabyte`. These are
routinely mangled and they matter, because action items hang off them.

### 4. Proper nouns

`Razorpay`, `Jenkins`, `Slack`, and the three names. Product names getting
garbled is a common and very visible failure.

### 5. Minutes quality

Once you enable minutes (`AUTO_GENERATE_MINUTES=true`), check that it found:

- **Decision:** launch moved to the thirtieth
- **Action items:** Rahul → migration; Ananya → load testing and regression
- **Open question:** who renews the Razorpay sandbox credentials
- It should *not* invent an owner for the Razorpay question — nobody took it.
