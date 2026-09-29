# Facebook image assets

Runtime image assets:

- `job-new-orange.png` — active/new vacancy.
- `job-deadline-yellow.png` — verified vacancy closing within 72 hours.
- `job-alert-blue.png` — candidate lists, results and other employment notices.
- `job-apply-red.png` — active vacancy with a verified application path.

Selection is semantic first, rotation second. A template key is pinned to the
article before upload, so retries cannot silently change the visual. Normal
vacancies rotate between truthful eligible variants while recent-template
memory reduces repetition.

The renderer normalizes the source artwork to the final Facebook output size.
Keep these filenames unchanged unless `job_visual_policy.py` and the renderer
mapping are updated together.
