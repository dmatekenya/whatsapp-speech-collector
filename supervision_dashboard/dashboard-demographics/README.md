# Contributor demographic charts (admin Overview)

This module displays four **project-wide** aggregate charts: Gender, Country, Age Group and First Language. It uses the already published, de-identified ZaZoAI public-progress JSON. It does **not** query contributor identities, write to PostgreSQL or alter QC decisions. Access to the chart proxy is limited to authenticated dashboard administrators.

Country is inferred from a recognized Malawi district only when a verified country field is absent; other district categories remain unverified. Age uses the upstream five-year group normalizer (`age` or `extra.age_group`). Categories with fewer than three contributors are already grouped by the upstream publisher.

The Overview placeholder is rendered directly by `app/pages.php`. Styles and JS are linked from `app/view.php` on the authenticated admin Overview. No additional installers or HTML string rewriting required.

Values refresh while the tab is visible approximately every five seconds, depending on upstream publication latency. The charts can be temporarily unavailable while the public dataset updates, without interrupting the QC dashboard.
