# Remote file management — 2026-10-21

Use matching GCS and Lab 8 source downloads from `feature/remote-file-management`.
The project owner confirmed the preceding upload build works on hardware. These
new delete/rename operations have passed automated tests; hardware testing is pending.

## Use the buttons

1. Upload the extracted Lab 8 project with PlatformIO environment
   `esp32thing_plus_115200` for the existing 115200 radios. The USB startup marker
   is `KFS1 manage-1`. The default `esp32thing_plus` environment remains 9600.
2. Launch the matching GCS executable. Keep the working radio configuration:
   satellite AP=0; ground AP=1 with GCS XBee API (AP=1) for RSSI, or ground AP=0
   with GCS Transparent. The existing legacy AO=2 API format remains supported.
3. Open **Remote Files**, browse to a folder and **Refresh / Open**. The firmware
   advertises `MANAGE1`, enabling **Rename...** and **Delete...**. Older firmware
   retains its existing download/upload capabilities but leaves these buttons disabled.
4. Select a file. **Rename...** asks for its new filename in the current folder.
   **Delete...** asks for confirmation of the full satellite path, defaulting to No.
   Deletion is permanent. Successful operations refresh the folder automatically.

Both controls reject folders, the root and reserved `.kfs-` upload names. Use
**Remove partial...** for reserved unfinished upload files. Rename cannot move
files or overwrite any existing destination, including case-insensitive aliases;
case-only renames are rejected. FAT filename restrictions apply. Paths remain
limited to 192 UTF-8 bytes. No operation can run during an active file session.
Cancel is disabled after dispatch because a completed rename/delete cannot be
undone. A confirmed result remains success if the subsequent folder refresh fails.

## Retry and result recovery

Requests have CRC32 protection and bounded retries using the same request ID.
The firmware retains the exact last management request and reply separately
from ordinary metadata queries, preventing retransmitted deletion from deleting
a newly created file under the same name. If all direct replies are lost, the
client asks for the operation's retained result. Confirmed errors remain errors;
unconfirmed completion explicitly says to Refresh before trying again.

Only the last management operation is retained in RAM. It survives idle expiry,
but a reset or a newer management operation replaces/loses it. After a reset,
disconnect or unknown result, inspect the refreshed folder before issuing another
operation. SD rename/delete are not power-loss transactions.

## Protocol additions

HELLO remains `HELLO|1|128`; CAPS becomes `CAPS|UPLOAD1|MANAGE1|128`.
Paths remain hex-encoded. Maximum frame buffer increases from 768 to 1024 bytes
so RENAME can carry two full-length paths; the data chunk remains 128 bytes.

| Request fields | Response fields |
| --- | --- |
| `DELETE|path_hex` | `DELETED` or `ERR|code` |
| `RENAME|source_hex|target_hex` | `RENAMED` or `ERR|code` |
| `RESULT|mutation_request_id_hex` | `RESULT|DELETE_or_RENAME|DELETED_or_RENAMED`, `RESULT|operation|ERR|code`, or `RESULT|UNKNOWN` |

Existing upload/download commands remain supported. The firmware rejects file
management while another file session is active. USB logs identify confirmed
renames/deletions; GCS diagnostics retain bounded operation metadata.

## Validation

All 12 GCS suites passed, including 16 remote-file tests and 13 XBee API tests.
The actual portable firmware service passed 24 host/PTY tests with the real GCS
client: lost acknowledgments, lost result replies, duplicate deletion after a
metadata query, idle expiry, permanent backend errors, full-length rename frames,
folder/path rejection, and existing-destination protection. Both 9600 and 115200
firmware environments compiled successfully. Windows packaging and source ZIP
checks run on the feature branches and on main.

On hardware, start with disposable files: rename one, download/compare it, then
delete it and Refresh. Verify that a rejected existing-name rename preserves both
files, cancelling the confirmation preserves the selected file, and folder
selection cannot delete/rename a directory. Repeat in your working API mode and
check that upload/download and their live graphs still behave as before.
