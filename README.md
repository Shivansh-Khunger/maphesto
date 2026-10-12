# Maphesto

Textual TUI for converting motion-photo groups and preparing size-limited local batches. Uploading stays outside Maphesto.

```sh
python3 -m pip install -r requirements.txt
./maphesto
```

## Controls

- `↑/↓` or `j/k`: move
- `←` or `h`: parent/back
- `→`, `l`, or `Enter`: open/activate
- `Space`: select directory
- `Esc`: cancel/back
- `Ctrl+C`: quit

Picker starts at process launch directory. Dotfiles stay hidden. Files appear dimmed with size and cannot be opened. Output automatically follows selected input's parent until explicitly changed.

Processing stages source files first, lets `motionphoto2` produce converted output, then packs converted/deferred output into 1 GiB batches. Dashboard shows group count, prepared file count, output bytes, errors, current item, and batch size. `P` pauses, `C` requests safe cancellation, and `L` opens full log.

## Safety and artifacts

Source media is never deleted or moved during processing. Each run writes media beneath:

```text
<output-base>/maphesto-<session-id>/
├── .work/            # in-progress/failed work; preserved on interruption
├── SOUL_NAME/        # verified completed batch files
└── INCOMPLETE        # present after cancellation or errors
```

Operational metadata lives separately:

```text
~/.maphesto/sessions/<session-id>/
├── summary.json
├── events.log
└── manifest.jsonl
```

Cleanup uses OS Trash/Recycle Bin through `send2trash`; permanent recursive deletion is not used. Protected paths are rejected.

Settings configure `motionphoto2` executable and batch byte limit (default 1 GiB).
