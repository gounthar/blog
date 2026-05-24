# Local development image for `docker compose up` (live `hugo server` preview).
#
# Mirrors the production build in .github/workflows/build.yml, which runs
# Hugo Extended plus `gem install asciidoctor rouge`. Asciidoctor is required
# because the site's posts are written in AsciiDoc.
#
# This replaces the previous `hugomods/hugo:exts` image. The `exts` variant was
# frozen upstream at exts-0.154.5 in January 2026, so it no longer tracks new
# Hugo releases. The plain image keeps shipping current Hugo Extended; we layer
# Asciidoctor on top to match the production toolchain. The base tag below is
# kept current automatically by updatecli (updatecli.d/hugo-image.yaml).
FROM hugomods/hugo:0.161.1

RUN apk add --no-cache ruby \
    && gem install asciidoctor rouge --no-document
