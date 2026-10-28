export const meta = {
  name: 'voxxy-voice-clone',
  description: 'Run `voxxy voice clone` for a described person or character, score the result against held-out real speech, and retry with another video when it is weak',
  whenToUse: 'To create a voice from a description with a quality check. args: {query, name?, threshold?, maxAttempts?}. threshold defaults to 0.6 ecapa, maxAttempts to 3.',
  phases: [
    { title: 'Clone', detail: 'voxxy voice clone, picking a candidate that is the target speaker alone' },
    { title: 'Score', detail: 'A/B harness against held-out speech of the same speaker, engine must be voxcpm' },
    { title: 'Retry', detail: 'another candidate or video while the score is under the threshold; keep the best' },
  ],
}

const REPO = '/home/delorenj/code/voxxy'
const WORKFLOWS_DOC = '/home/delorenj/code/skillex/all-skills/vox-tts/references/voice-workflows.md'
const QUALITY_DOC = '/home/delorenj/code/skillex/all-skills/vox-tts/references/reference-clip-quality.md'
const SCORER = '/home/delorenj/code/skillex/all-skills/vox-tts/scripts/score_voice_candidates.py'

const query = args && typeof args === 'object' ? args.query : (typeof args === 'string' ? args : '')
if (!query) {
  return { error: 'args.query is required, e.g. {query: "the dog from Family Guy", name: "brian"}' }
}
const wantedName = args && typeof args === 'object' && args.name ? args.name : null
const THRESHOLD = args && typeof args === 'object' && typeof args.threshold === 'number' ? args.threshold : 0.6
const MAX_ATTEMPTS = args && typeof args === 'object' && typeof args.maxAttempts === 'number' ? args.maxAttempts : 3

const COMMON = `
You work on the voxxy TTS service (repo ${REPO}, CLI \`voxxy\`, live server https://vox.delo.sh).
Read ${WORKFLOWS_DOC} (workflow A) and ${QUALITY_DOC} first.
Hard rules: run whisper / \`voxxy voice clone\` / the scorer through \`heavy <cmd>\`; only delete ztmp-* voices YOU created, via \`voxxy voice delete <name> --yes\`; never edit files in ${REPO}/voices by hand; a scored line only counts if engine is voxcpm or vibevoice; never write secrets into files; do not git commit or push.
`

const CLONE = {
  type: 'object',
  properties: {
    voice: { type: 'string' },
    exitCode: { type: 'integer' },
    workdir: { type: 'string', description: 'absolute ~/.cache/voxxy/clone/<slug> directory' },
    identity: { type: 'string' },
    videoUrl: { type: 'string' },
    clipPath: { type: 'string', description: 'absolute path of the exported clip wav' },
    window: { type: 'string' },
    transcript: { type: 'string' },
    engine: { type: 'string' },
    personaId: { type: 'string' },
    triedUrls: { type: 'array', items: { type: 'string' } },
    note: { type: 'string' },
  },
  required: ['voice', 'exitCode', 'workdir', 'identity', 'videoUrl', 'clipPath', 'window', 'transcript', 'engine', 'personaId', 'triedUrls', 'note'],
}

const SCORE = {
  type: 'object',
  properties: {
    voice: { type: 'string' },
    ecapa: { type: ['number', 'null'] },
    wer: { type: ['number', 'null'] },
    f0: { type: ['number', 'null'] },
    referenceF0: { type: ['number', 'null'] },
    reference: { type: 'string' },
    heldOut: { type: 'boolean' },
    engineOk: { type: 'boolean' },
    note: { type: 'string' },
  },
  required: ['voice', 'ecapa', 'wer', 'f0', 'referenceF0', 'reference', 'heldOut', 'engineOk', 'note'],
}

const tried = []
const attempts = []
let best = null
let reference = null

for (let n = 1; n <= MAX_ATTEMPTS; n++) {
  const first = n === 1
  phase(first ? 'Clone' : 'Retry')
  const liveName = wantedName
  const cloneTask = first
    ? `Run: heavy voxxy voice clone ${JSON.stringify(query)} --yes ${liveName ? '--name ' + liveName + ' ' : ''}--force
If it exits 5 (needs --pick) read <workdir>/markers.json and the candidate table, choose the candidate whose transcript is clearly the TARGET speaker alone (not a host, not dialogue, not a montage, natural pitch), and re-run with --pick N (cached download, cheap). If the identity is wrong or low confidence, say so in note and retry with a better --name / query.`
    : `A previous attempt scored under ${THRESHOLD}. URLs already tried: ${JSON.stringify(tried)}.
Make a DIFFERENT attempt without disturbing the live voice: run
  heavy voxxy voice clone ${JSON.stringify(query)} --yes --name ztmp-${liveName || 'clone'}-r${n} --no-persona --force --pick <N>
choosing another candidate from the same workdir's markers.json when one is a clearly better single-speaker window; if the video itself is the problem (wrong speaker, montage, pitch-shifted, bad audio), use a refined query (add the voice actor's name plus "interview" / "audiobook" / one scene) so a different video ranks first. Never reuse a URL in the tried list.`
  const c = await agent(
    `${COMMON}
${cloneTask}
Report the voice name that was uploaded, the workdir, the video URL, the exported clip path (clip.json in the workdir has it), the window, the exact prompt_text transcript, the engine the verification line used, and the persona id if one was associated. Put every video URL used so far in triedUrls.`,
    { label: `clone attempt ${n}`, phase: first ? 'Clone' : 'Retry', schema: CLONE },
  )
  if (!c || c.exitCode === 1 || c.exitCode === 3) {
    attempts.push({ attempt: n, clone: c, score: null })
    log(`attempt ${n}: clone failed (${c ? 'exit ' + c.exitCode : 'no result'})`)
    continue
  }
  for (const u of c.triedUrls || []) if (!tried.includes(u)) tried.push(u)
  if (c.videoUrl && !tried.includes(c.videoUrl)) tried.push(c.videoUrl)

  phase('Score')
  const s = await agent(
    `${COMMON}
Score the voice "${c.voice}" (just cloned from ${c.videoUrl}, window ${c.window}, workdir ${c.workdir}).
${reference ? `Use the SAME reference as the earlier attempts so scores are comparable: ${reference}.` : `Make a held-out reference: from ${c.workdir}/source.wav cut a 30-60 s span of the target speaker that does NOT overlap the clip window ${c.window} (stay at least 5 s away), re-encode with ffmpeg (never -c copy) to ${c.workdir}/reference.wav. Prefer a span of continuous target speech; if the source has too little other speech, say heldOut=false.`}
Run: heavy ${SCORER} --reference <reference.wav> --lines 4 --json ${c.workdir}/score.json ${c.voice}
Report ecapa, wer, f0, the reference's own f0, the reference path, and whether every line came from voxcpm. If any line was elevenlabs the voice is NOT a clone: say so, engineOk=false, and check \`voxxy daemon status\` / ollama VRAM in note.`,
    { label: `score attempt ${n}`, phase: 'Score', schema: SCORE },
  )
  if (s && !reference) reference = s.reference
  const entry = { attempt: n, clone: c, score: s }
  attempts.push(entry)
  if (s && s.engineOk && typeof s.ecapa === 'number' && (!best || s.ecapa > best.score.ecapa + 0.03)) best = entry
  log(`attempt ${n}: ${c.voice} ecapa ${s ? s.ecapa : 'n/a'} (threshold ${THRESHOLD})`)
  if (s && s.engineOk && typeof s.ecapa === 'number' && s.ecapa >= THRESHOLD) break
}

phase('Retry')
let finalNote = 'no attempt produced a scoreable clone'
if (best) {
  const finish = await agent(
    `${COMMON}
Finalize the clone of ${JSON.stringify(query)}. Attempts so far:
${JSON.stringify(attempts, null, 2)}
Best attempt: ${best.attempt} (ecapa ${best.score.ecapa}).
1. If the best attempt used a temp voice (name starting ztmp-) and the live voice ${wantedName || best.clone.voice.replace(/^ztmp-/, '').replace(/-r\d+$/, '')} is a different, worse attempt, install the best clip over the live voice: \`voxxy voice add <best clip path> --name <live voice> --prompt-text "<exact transcript>" --no-prompt\` (this keeps the persona). Then, if no persona is associated, run \`voxxy persona generate\` / \`voxxy voice persona\` as in ${WORKFLOWS_DOC} so the live voice has one.
2. Delete every ztmp-* voice created by this run (\`voxxy voice delete <name> --yes\`) and confirm with \`voxxy voice list\`.
3. Confirm the live voice answers with engine voxcpm via POST /synthesize-url, and that prompt_text and persona are set.
Return one paragraph: live voice name, final ecapa and whether it reached ${THRESHOLD}, the source video, and anything a human should check (wrong-speaker risk, non-held-out reference, pitch).`,
    { label: 'finalize', phase: 'Retry' },
  )
  finalNote = finish
}

return { query, threshold: THRESHOLD, bestAttempt: best ? best.attempt : null, bestEcapa: best ? best.score.ecapa : null, reachedThreshold: !!(best && best.score.ecapa >= THRESHOLD), attempts, summary: finalNote }
