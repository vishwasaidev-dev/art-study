# Art Study

A deep-zoom study viewer for public-domain artworks. Search an artist, open any
work, and zoom to the brushstroke — with the tools a student or critic actually
uses:

- **Full catalogue record** for every work (medium, credit line, provenance,
  inscriptions, curatorial note) and a copy-ready **Chicago citation**
- **Compare mode** — two linked deep-zoom panels with synchronized pan/zoom
- **Composition overlays** — rule of thirds, golden section, diagonal armature,
  centre lines (locked to the image), plus a **scale bar** from the object's
  real dimensions
- **Analysis filters** — grayscale, high-contrast, sharpen, edge-detect, invert
- **Marks** — pin a zoom position with a note; **notes & tags** per work
- **Study sets** → export a captioned, cited worksheet (print / save as PDF)
- **Other impressions** — find the same work across the other collections

## Sources

All public-domain / open-access, proxied server-side (so Cloudflare, referer
checks and CORS never reach the browser):

| Collection | API | Key |
|---|---|---|
| [Art Institute of Chicago](https://api.artic.edu/docs/) | REST + IIIF | none |
| [Cleveland Museum of Art](https://openaccess-api.clevelandart.org/) | REST | none |
| [Harvard Art Museums](https://github.com/harvardartmuseums/api-docs) | REST + IIIF | free ([get one](https://harvardartmuseums.org/collections/api)) |
| [The Metropolitan Museum of Art](https://metmuseum.github.io/) | REST | none |

## Run locally

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
export HARVARD_API_KEY=your-key        # optional; enables the Harvard source
python run_local.py                    # opens http://127.0.0.1:8730/
```

## Deploy

Any host that runs `uvicorn app:app`. The `Procfile` and `.python-version` are
set up for Heroku:

```bash
heroku create
heroku config:set HARVARD_API_KEY=your-key
git push heroku main
```

Notes and study sets are saved to `study_session.json` **and** mirrored to the
browser's `localStorage`, so they survive an ephemeral host cycling its
filesystem.

## Stack

Starlette + uvicorn, [OpenSeadragon](https://openseadragon.github.io/) for the
tiled deep-zoom viewer, Pillow for transcoding the odd TIFF master. No build
step, no framework.
