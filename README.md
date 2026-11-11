Plain-text backups of the published posts at https://daeus.blog/

The backups are generated from the public WordPress API by
`scripts/sync_blog.py`. The script preserves the visible headings, prose,
lists, quotes, and tables in a deterministic text format. The post “My Top 10
Memes Of The Year (2020-2024)” is intentionally excluded.

GitHub Actions checks the site daily and commits any changed or newly
published posts. To check or refresh locally:

```sh
python scripts/sync_blog.py --check
python scripts/sync_blog.py
```
