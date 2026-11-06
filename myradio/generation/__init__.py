"""Generate-ahead engine for YuE2 music and Drama 3 speech, shipped as the ``generation`` extension.

Read ``docs/lanes/generation.md`` for the contract. The extension itself lives in
``myradio.generation.extension`` (``build()`` returns it); nothing is imported here so that importing the
package stays cheap and cannot fail.
"""
