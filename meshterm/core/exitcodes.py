"""The exit statuses ``meshterm`` returns, and what each one means.

A scripted run's *return value is its report*: the CLI says what happened in plain
text on stdout and says whether it worked in ``$?``. The codes are classified rather
than a bare 0/1 so a caller can tell the failures apart without reading prose — a
timeout is worth retrying, a bad key never is.

The set is small and closed. Anything that does not map onto one of the specific
causes below is :data:`FAILURE`; nothing invents a code of its own.

+------+--------------+--------------------------------------------------------+
| Code | Name         | Meaning                                                |
+======+==============+========================================================+
| 0    | ``OK``       | The command did what it was asked.                     |
+------+--------------+--------------------------------------------------------+
| 1    | ``FAILURE``  | It failed for a reason with no more specific code —    |
|      |              | a bad configuration or preference value, an            |
|      |              | unreadable file, an unhandled fault.                   |
+------+--------------+--------------------------------------------------------+
| 2    | ``USAGE``    | The command line itself was wrong: an unknown flag, a  |
|      |              | missing argument, a value the parser rejected. Click   |
|      |              | returns this on its own; it is named here so the       |
|      |              | table is complete.                                     |
+------+--------------+--------------------------------------------------------+
| 3    | ``NO_DEVICE``| No companion could be selected: none attached, none    |
|      |              | matching ``--port``/``--ble``/``--tcp``, or the        |
|      |              | choice was ambiguous. Nothing was transmitted.         |
+------+--------------+--------------------------------------------------------+
| 4    | ``DEVICE``   | A device was reached but the operation failed: a       |
|      |              | command error, a timeout, a link lost mid-run.         |
|      |              | Retrying is reasonable.                                |
+------+--------------+--------------------------------------------------------+
| 5    | ``NO_RESULT``| The command ran and completed, and there was nothing   |
|      |              | to report: an empty list, a target that never          |
|      |              | answered, a conversation with no messages. stdout is   |
|      |              | empty; this is not an error.                           |
+------+--------------+--------------------------------------------------------+

:data:`NO_RESULT` is the one that needs saying out loud, because it is the one a
script most wants and no other tool offers: ``grep`` conflates "found nothing" with
"worked", and a caller that has to count output lines to tell an empty mesh from a
full one is parsing when it could be branching.
"""

from __future__ import annotations

#: The command did what it was asked.
OK = 0

#: A failure with no more specific code — a bad value, an unreadable file, a fault.
FAILURE = 1

#: The command line was wrong. Click returns this itself; named here for completeness.
USAGE = 2

#: No companion device could be selected. Nothing was transmitted.
NO_DEVICE = 3

#: A device was reached but the operation failed. Retrying is reasonable.
DEVICE = 4

#: The command completed with nothing to report. Not an error; stdout is empty.
NO_RESULT = 5

#: Every code, by name, for the ``--help`` epilog and the documentation.
MEANINGS: dict[int, str] = {
    OK: "success",
    FAILURE: "failure",
    USAGE: "usage error",
    NO_DEVICE: "no device found, or the selection was ambiguous",
    DEVICE: "the device was reached but the operation failed",
    NO_RESULT: "nothing to report (empty result)",
}
