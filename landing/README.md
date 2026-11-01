# Landing page

The landing page for whichsdk. It is static files with no build step.

Preview it from the repo root:

    python -m http.server --directory landing

Then open http://localhost:8000.

To deploy it, point any static host at this folder. On Vercel, Netlify and Cloudflare Pages, set the root or publish directory to `landing`. GitHub Pages only serves the repo root or `docs`, so it needs a workflow that publishes this folder.

The numbers on the page are the results of the study `auth-nextjs-v3`: 50 runs of Claude Code, finished on 2026-10-31. They come from the JSON that `uv run whichsdk report studies/auth-nextjs.yaml --out runs/report-v3.html` writes next to the HTML. They are written into the page in several places: the hero bars, the chart and its table view, the task rows, the verdict squares and list, and a few sentences. After a new study, change all of them together.

The three runs in the hero are real runs of that study. The task text comes from `studies/auth-nextjs.yaml`. Each quote is copied from the run's `transcript.jsonl`. Each evidence line is copied from the run's `changes.diff`, and "same as the baseline" means the run's `package.json` is identical to its baseline copy.

Fonts load from Google Fonts. The two sky images in `img` were rendered with a WebGL shader. They are not photographs.

The page uses DataFast for visitor counts.
