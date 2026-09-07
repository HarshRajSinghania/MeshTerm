# Security policy

## Reporting a vulnerability

Use GitHub's private vulnerability reporting: go to the **Security** tab of this
repository and choose **Report a vulnerability**. That opens a private thread visible only
to you and the maintainer, so nothing is disclosed while it is being fixed.

If that is unavailable to you for any reason, email <johnputer@meshterm.net> instead.

**Please do not open a public issue for a security problem.**

## What to expect

MeshTerm is maintained by one person in his spare time, so this promises only what can
actually be kept:

- **An acknowledgement within a week.** Not a fix within a week — an answer saying the
  report was read and what happens next.
- **A fix or a decision within 90 days** for anything confirmed. If it will take longer,
  you will be told why.
- **Credit in the release notes**, unless you would rather not be named.

A slow reply is not a dismissal. If a week passes with silence, a nudge on the same thread
is welcome.

## Supported versions

While the major version is `0`, only the **latest release** is supported. There are no
backports; a fix ships in the next version.

## Scope

In scope — anything in this repository:

- Code execution, path traversal, or file overwrite triggered by data MeshTerm reads:
  packets overheard from the mesh, a companion's responses, a config or preferences file,
  a downloaded map tile.
- Anything that discloses a **channel key**, a **repeater admin password**, or a node's
  private key material — including into the local database, a log, an export, or the
  screen when it should not be there.
- Anything that lets a crafted packet corrupt or delete the observation database.
- Weaknesses in how MeshTerm stores credentials on disk.

Out of scope, though still worth telling us about somewhere:

- Vulnerabilities in the **MeshCore firmware** or in the companion device itself. Those
  belong upstream at the MeshCore project.
- Vulnerabilities in the `meshcore` Python library, `bleak`, or any other dependency —
  report those to their maintainers. If a MeshTerm change can mitigate one, we want to
  know.
- The fact that anyone within radio range can hear a public channel. That is how a mesh
  works, not a defect.
- Anything requiring an attacker to already have write access to your filesystem or your
  companion device.

## A note on what MeshTerm handles

MeshTerm decrypts channel traffic using keys **you** give it, and it records everything it
overhears to a local SQLite database. That database is not encrypted. Treat it as you would
any file holding your mesh's history, and remember that a backup of it carries the same
weight.
