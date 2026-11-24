# Studies

A study is a collection activity with its own rules for what participants
record and how those recordings are used. The main study collects speech for
the production dataset. A study such as QC calibration collects recordings
under controlled conditions for a specific research question.

A participant can have more than one enrollment, but only one can be active
at a time. An enrollment records the participant's study and round. To switch
a participant to QC calibration, an administrator pauses the active main
enrollment and activates the calibration enrollment. When the participant
finishes calibration, the bot resumes the paused main enrollment if one exists.

Calibration clips are excluded from main collection progress, lifetime minutes,
and prompt coverage. The recordings still retain their calibration enrollment
and study metadata for review. See [Enrollments and study data](../platform/database.md)
for details.

The QC calibration [recording protocol](qc-calibration/protocol.md) describes
the ten conditions and recording tasks. The [methodology](qc-calibration/methodology.md)
describes the collection targets. The [audio-quality archive](qc-calibration/archive/handover.md)
preserves the earlier handover, recording guide, and labelling plan.
