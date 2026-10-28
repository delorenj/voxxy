export const meta = {
  name: 'voxxy-voice-upgrade',
  description: 'Upgrade voxxy voices to clean sentence-boundary reference clips with an exact prompt_text, A/B scored per voice, then summarize before/after',
  whenToUse: 'After a voxxy cloning-quality change, or when voices in `voxxy voice list --json` lack prompt_text. args: optional array of voice names; default is every voice.',
  phases: [
    { title: 'Plan', detail: 'list voices, take one dated backup of every clip and the voice list' },
    { title: 'Upgrade', detail: 'one agent per voice (4 at a time) following workflow C: candidates, ztmp voices, score, install, clean up' },
    { title: 'Summarize', detail: 'merge before/after scores, flag kept/failed voices, check for leftover ztmp voices' },
  ],
}

const REPO = '/home/delorenj/code/voxxy'
const WORKFLOWS_DOC = '/home/delorenj/code/skillex/all-skills/vox-tts/references/voice-workflows.md'
const QUALITY_DOC = '/home/delorenj/code/skillex/all-skills/vox-tts/references/reference-clip-quality.md'
const SCORER = '/home/delorenj/code/skillex/all-skills/vox-tts/scripts/score_voice_candidates.py'
const CONCURRENCY = 4

const COMMON = `
You work on the voxxy TTS service (repo ${REPO}, CLI \`voxxy\`, live server https://vox.delo.sh).
Read ${WORKFLOWS_DOC} (workflow C) and ${QUALITY_DOC} before acting; they are the procedure and the decision rules.
Hard rules:
- Run anything CPU-heavy that lasts over a minute (whisper, \`voxxy voice candidates\`, ${SCORER}) through \`heavy <cmd>\`.
- Work from BACKUP copies of clips, never the live ${REPO}/voices files. Never delete anything in ${REPO}/voices by hand.
- Temp voices are named ztmp-<voice>-<variant>. Delete only the ztmp-* voices YOU created, with \`voxxy voice delete <name> --yes\`.
- A line only counts if the engine was voxcpm (or vibevoice). An elevenlabs line is not a clone.
- Never write any key or secret into a file. Do not git commit or push.
`

const PLAN = {
  type: 'object',
  properties: {
    voices: { type: 'array', items: { type: 'string' } },
    backupDir: { type: 'string' },
    skipped: { type: 'array', items: { type: 'string' } },
    notes: { type: 'string' },
  },
  required: ['voices', 'backupDir', 'skipped', 'notes'],
}

const RESULT = {
  type: 'object',
  properties: {
    voice: { type: 'string' },
    action: { type: 'string', enum: ['installed', 'kept', 'skipped', 'failed'] },
    hadPromptTextBefore: { type: 'boolean' },
    source: { type: 'string', description: 'what the clean clip was cut from (path or url) and the window in seconds' },
    reference: { type: 'string', description: 'recording of the real speaker the scores are measured against; say if it is not held-out' },
    before: {
      type: 'object',
      properties: { ecapa: { type: ['number', 'null'] }, wer: { type: ['number', 'null'] }, f0: { type: ['number', 'null'] } },
      required: ['ecapa', 'wer', 'f0'],
    },
    after: {
      type: 'object',
      properties: { ecapa: { type: ['number', 'null'] }, wer: { type: ['number', 'null'] }, f0: { type: ['number', 'null'] } },
      required: ['ecapa', 'wer', 'f0'],
    },
    referenceF0: { type: ['number', 'null'] },
    engineOk: { type: 'boolean', description: 'every scored line came from voxcpm or vibevoice' },
    promptTextNow: { type: 'string' },
    tempVoicesLeft: { type: 'array', items: { type: 'string' } },
    note: { type: 'string' },
  },
  required: ['voice', 'action', 'hadPromptTextBefore', 'source', 'reference', 'before', 'after', 'referenceF0', 'engineOk', 'promptTextNow', 'tempVoicesLeft', 'note'],
}

const wanted = Array.isArray(args) ? args : (args && Array.isArray(args.voices) ? args.voices : null)

phase('Plan')
const plan = await agent(
  `${COMMON}
Task: prepare a batch voice upgrade.
1. Run \`voxxy voice list --json\`. Voices to process: ${wanted && wanted.length ? 'exactly these names, if they exist: ' + JSON.stringify(wanted) : 'every voice except names starting with ztmp-'}. Put any requested name that does not exist, and any ztmp-* voice, in "skipped".
2. Take ONE dated backup: B=~/.local/share/voxxy/voice-backups/$(date +%F)-batch-upgrade ; mkdir -p "$B"; save the voice list JSON as "$B/voices.json"; copy every processed voice's wav from ${REPO}/voices/<name>.wav into "$B" (cp -n; reading root-owned files is fine). Report the expanded absolute path as backupDir.
3. Confirm the engine is healthy: \`voxxy health\` and, if voxcpm is not ready, say so in notes (do not fix it).
Return the voice names and the backup dir.`,
  { label: 'plan + backup', phase: 'Plan', schema: PLAN },
)

if (!plan || !plan.voices || plan.voices.length === 0) {
  log('no voices to upgrade')
  return { plan, results: [], summary: 'no voices to upgrade' }
}
log(`${plan.voices.length} voice(s), backup at ${plan.backupDir}; skipped: ${JSON.stringify(plan.skipped)}`)

phase('Upgrade')
// A fixed pool of CONCURRENCY lanes; voice i goes to lane i % CONCURRENCY and each lane runs its voices one after another.
const lanes = Array.from({ length: Math.min(CONCURRENCY, plan.voices.length) }, () => [])
plan.voices.forEach((v, i) => lanes[i % lanes.length].push(v))

const laneResults = await parallel(
  lanes.map((lane, laneIdx) => async () => {
    const out = []
    for (const voice of lane) {
      const r = await agent(
        `${COMMON}
Upgrade exactly ONE voice: "${voice}". Follow workflow C in ${WORKFLOWS_DOC} step by step.
The batch backup (clips + voices.json) is at ${plan.backupDir}; step 1 is already done for the batch, but copy this voice's clip there if it is missing.
Other agents upgrade other voices in parallel (lane ${laneIdx + 1} of ${lanes.length}); touch only ztmp-${voice}-* temp voices and the live voice "${voice}".
Decision rule (reference-clip-quality section 5): install a candidate only if its ecapa beats the incumbent by more than 0.03 with no clearly worse WER and no f0 drift, or at an ecapa tie if it adds a missing prompt_text. Otherwise keep the incumbent and say why.
If there is no usable source or no window passes the gates, action is "kept" (or "skipped" with a reason), not "failed". "failed" is for tool errors you could not work around.
Before/after numbers come from the scoring harness (${SCORER}); use null for a number you could not measure.
When you finish, ${voice} must be installed or untouched, every ztmp-${voice}-* voice you made must be deleted, and tempVoicesLeft must list any you could not delete.`,
        { label: `upgrade ${voice}`, phase: 'Upgrade', schema: RESULT },
      )
      out.push(r || { voice, action: 'failed', hadPromptTextBefore: false, source: '', reference: '', before: { ecapa: null, wer: null, f0: null }, after: { ecapa: null, wer: null, f0: null }, referenceF0: null, engineOk: false, promptTextNow: '', tempVoicesLeft: [], note: 'agent returned nothing (skipped or died)' })
    }
    return out
  }),
)

const results = laneResults.filter(Boolean).flat()
log(`upgraded ${results.filter((r) => r.action === 'installed').length}, kept ${results.filter((r) => r.action === 'kept').length}, skipped ${results.filter((r) => r.action === 'skipped').length}, failed ${results.filter((r) => r.action === 'failed').length}`)

phase('Summarize')
const summary = await agent(
  `${COMMON}
You are the summarizer for a batch voice upgrade. Per-voice reports (JSON):
${JSON.stringify(results, null, 2)}

Skipped before the fan-out: ${JSON.stringify(plan.skipped)}. Backup: ${plan.backupDir}.
1. Run \`voxxy voice list --json\` and \`voxxy voice list\`. Check no ztmp-* voices remain; if some remain AND a worker report lists them in tempVoicesLeft, delete exactly those with \`voxxy voice delete <name> --yes\`; report any other ztmp-* voice without deleting it.
2. Cross-check every "installed" voice: prompt_text is now non-empty in the live list and equals promptTextNow, persona unchanged.
3. Write a markdown report: a before/after table (voice | action | ecapa before | ecapa after | delta | wer after | f0 after vs reference f0 | note), then three short lists: voices still without prompt_text, voices kept and why, anything needing a human (wrong speaker suspected, pitch drift, non-held-out reference, engine not voxcpm). Do not call a batch "all green" unless every row is installed with a delta above 0.03 or has a stated reason to be kept.
Return only the markdown.`,
  { label: 'summary', phase: 'Summarize' },
)

return { backupDir: plan.backupDir, skipped: plan.skipped, results, summary }
