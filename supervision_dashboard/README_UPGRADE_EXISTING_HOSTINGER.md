# Ver. 0.0.0.1 — complete website replacement with actual four prompt types

This archive replaces the entire Hostinger Masakhane application source tree, not just a partial patch. Keep your existing `private/config.php`, private account data and uploaded avatars.

See the outer package `README_START_HERE.md` before deployment. Never run this on the DigitalOcean collector folder.

- Filter and classify from `assignments.prompt_type`: sentence, image, free_speech, conversation_starter.
- Only the restricted DigitalOcean gateway can supply types, not a guessed filename label.
- Four colored donuts show each prompt type's share of total classified recorded duration. The right-hand breakdown now shows accepted-duration totals and proportions by type. The project-wide donut still counts only eligible, officially accepted audio toward the 500-hour goal.
- Only Approve and Reject are available as official decisions. After successful DigitalOcean confirmation, the next assigned pending recording opens automatically. Reviewer notes remain available even before write-back is activated.
- Contributors have permission-scoped detail pages with stored count, recorded/accepted/rejected duration, and QC status totals.
- Review decisions remain auditable and require a production-owner-authorized QC connection.
- No DB reset; `php tools/upgrade_online.php` is idempotent and preserves existing accounts, permissions and reviews.
- CSS cache version updated for all users so chart formatting loads correctly.
