# Landing page

The landing page for whichsdk. It is static files with no build step.

Preview it from the repo root:

    python -m http.server --directory landing

Then open http://localhost:8000.

To deploy it, point any static host at this folder. On Vercel, Netlify and Cloudflare Pages, set the root or publish directory to `landing`. GitHub Pages only serves the repo root or `docs`, so it needs a workflow that publishes this folder.

Every number on the page is sample data, and the page says so. The study has not run. When it has, replace the counts and remove the sample labels together. The counts are written into the HTML in several places: the chart and its table view, the hero bars, the verdict squares and list, and a few sentences.

The three runs in the hero are illustrative and tagged that way. Their task text comes from `studies/auth-nextjs.yaml`.

Fonts load from Google Fonts. The two sky images in `img` were rendered with a WebGL shader. They are not photographs.
