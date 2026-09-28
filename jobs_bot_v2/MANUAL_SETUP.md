# Jobs v2 — manual assets needed from the owner

The code can run without these assets, but these manual files will make the
Facebook output look much more professional.

## 1. Four job-card backgrounds

Create four PNG files at 1600×900:

- `jobs_bot_v2/assets/job1.png`
- `jobs_bot_v2/assets/job2.png`
- `jobs_bot_v2/assets/job3.png`
- `jobs_bot_v2/assets/job4.png`

Rules:
- Keep the logo/brand fixed in the template.
- Do not write a permanent headline inside the background.
- Leave the central/lower reading area clean for the bot's dynamic Arabic title.
- Avoid busy photos behind the title.
- Make the four variants visually related but not identical.
- The bot will rotate them deterministically, as the working news bot does.
- If these files are missing, the engine safely falls back to a generated black card with a colored frame.

## 2. Do not manually create article images for every job

The bot should first try the official job/company page image. If no good image
exists, it should use the branded fallback card. This keeps quality consistent
without adding daily manual work.

## 3. Publishing credentials

Keep credentials only in GitHub Secrets. Never put Facebook tokens, Blogger
tokens, AI keys or app secrets in the repository.

## 4. Go-live switches

Jobs v2 is intentionally preview-only until the integration is complete:

- `JOBS_LIVE_PUBLISH=false`
- `JOBS_LIVE_FACEBOOK=false`

They should only be enabled after one full end-to-end preview is approved.
