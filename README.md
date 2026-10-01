# Jobs publishing bot

This repository publishes verified job opportunities to Blogger and follows each successful article with a Facebook Page post. General technology/news ingestion, translation, category rotation and local-file publishing have been removed.

- Official publication time must prove a job is at most **12 hours old**, including a final check immediately before a new live Blogger post.
- Fresh cyber security, IT, developer and internship/student opportunities receive priority. Other valid jobs remain eligible.
- Durable source identities, canonical URLs, job identity and campaign memory prevent rediscovery and duplicate publication.
- Official PDF pages become sequential images inside the article. Work resumes across bounded passes until all documents/pages finish; original download links remain visible.
- Employer logos and four fact-based Facebook backgrounds are preserved. Social errors retry independently without recreating the Blogger article.

`sources.json` is the source configuration. `jobs_article_queue.json` and `data/job_memory/` are durable state, not disposable caches.

```bash
python -m pip install -r requirements.txt
python main.py deployment-check
python main.py auto-cycle
# Optional local runner; GitHub Actions does not require a running PC:
python main.py auto-cycle --loop
python -m unittest discover -s tests -v
```

Running `python main.py` also runs one Jobs cycle. Configure the intended Blogger target and credentials before running publishing commands. Retired news commands are rejected.

See [deployment](DEPLOYMENT.md) and [cycle policy](docs/publishing-schedule.md) for credentials, scheduling, recovery and delivery behavior.
