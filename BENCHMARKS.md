# Stetwise benchmarks

One run of `python3 scripts/benchmark.py` on this machine. These are measurements, not a capacity promise.
No prices are recorded here. The numbers are the CPU time and peak memory of one file operation, so a monthly edit cap can be compared with CPU time.

- Python 3.12.3 on Linux-6.12.94+-x86_64-with-glibc2.39
- CPUs: 4
- MemTotal: 16387416 kB
- Engine import baseline peak RSS: 25.0 MiB

Samples are ZIP_STORED packages. Most of the file is worksheet or document XML, padded so the package lands on the target size. The edit is one cell (`A1` on sheet `Bench`) or one Word token (`BENCHWORD`).

- `inspect` parses the package and lists sheets or paragraphs.
- `propose` builds the edited package.
- `apply` is the verify step the server runs before publishing: it compares compressed part bytes and refuses an edit that touched a part it should have left alone. It does not repeat propose.
- `restore` copies the stored original bytes and hashes them. It does not parse XML. Publishing the restored version is a metadata write.

CPU seconds are `time.process_time` inside the worker, after imports. Peak memory is the worker's VmHWM after the operation, including the interpreter. That counter starts when the worker process starts, so it is not the parent that built the sample.

| Sample | Kind | Bytes | Op | OK | CPU seconds | Peak MiB | Wall seconds |
|---|---|---:|---|---|---:|---:|---:|
| small | xlsx | 102432 | inspect | yes | 0.0068 | 27.0 | 0.0611 |
| small | xlsx | 102432 | propose | yes | 0.0070 | 26.5 | 0.0646 |
| small | xlsx | 102432 | apply | yes | 0.0006 | 25.0 | 0.0556 |
| small | xlsx | 102432 | restore | yes | 0.0001 | 24.9 | 0.0534 |
| small | docx | 102436 | inspect | yes | 0.0060 | 25.8 | 0.0597 |
| small | docx | 102436 | propose | yes | 0.0071 | 25.8 | 0.0635 |
| small | docx | 102436 | apply | yes | 0.0005 | 25.4 | 0.0563 |
| small | docx | 102436 | restore | yes | 0.0001 | 25.0 | 0.0523 |
| medium | xlsx | 2097184 | inspect | yes | 0.1382 | 63.1 | 0.1945 |
| medium | xlsx | 2097184 | propose | yes | 0.1229 | 53.3 | 0.1804 |
| medium | xlsx | 2097184 | apply | yes | 0.0086 | 34.6 | 0.0652 |
| medium | xlsx | 2097184 | restore | yes | 0.0024 | 26.7 | 0.0587 |
| medium | docx | 2097188 | inspect | yes | 0.1327 | 44.1 | 0.1883 |
| medium | docx | 2097188 | propose | yes | 0.1370 | 46.3 | 0.1981 |
| medium | docx | 2097188 | apply | yes | 0.0083 | 34.6 | 0.0645 |
| medium | docx | 2097188 | restore | yes | 0.0022 | 27.0 | 0.0574 |
| large-5mb | xlsx | 5242912 | inspect | yes | 0.3720 | 116.5 | 0.4315 |
| large-5mb | xlsx | 5242912 | propose | yes | 0.3056 | 96.6 | 0.3625 |
| large-5mb | xlsx | 5242912 | apply | yes | 0.0182 | 49.5 | 0.0752 |
| large-5mb | xlsx | 5242912 | restore | yes | 0.0054 | 29.8 | 0.0633 |
| large-5mb | docx | 5242916 | inspect | yes | 0.3194 | 73.8 | 0.3790 |
| large-5mb | docx | 5242916 | propose | yes | 0.3446 | 77.4 | 0.4018 |
| large-5mb | docx | 5242916 | apply | yes | 0.0201 | 49.6 | 0.0817 |
| large-5mb | docx | 5242916 | restore | yes | 0.0056 | 29.6 | 0.0716 |
| large-25mb | xlsx | 26214432 | inspect | yes | 2.1992 | 479.1 | 2.2675 |
| large-25mb | xlsx | 26214432 | propose | yes | 1.7648 | 383.5 | 1.8302 |
| large-25mb | xlsx | 26214432 | apply | yes | 0.0968 | 150.3 | 0.1555 |
| large-25mb | xlsx | 26214432 | restore | yes | 0.0256 | 49.5 | 0.0833 |
| large-25mb | docx | 26214436 | inspect | yes | 1.8188 | 270.1 | 1.9158 |
| large-25mb | docx | 26214436 | propose | yes | 1.9199 | 286.2 | 1.9845 |
| large-25mb | docx | 26214436 | apply | yes | 0.0984 | 150.6 | 0.1584 |
| large-25mb | docx | 26214436 | restore | yes | 0.0269 | 50.0 | 0.0878 |

Every measured operation completed.

## Recalculate

Formula workbooks are ZIP_STORED. Each cell is `ROW()` plus a few zero terms, ten columns wide, with no cached value. `recalculate` starts LibreOffice, reads formula results in bulk, and patches cached values in one pass. The product time limit stays 60 seconds.

CPU seconds are the process tree (the worker plus LibreOffice) sampled while the call runs. Peak MiB is the highest resident set of that tree at one sample. Wall seconds include LibreOffice startup, calculation, and the XML patch.

| Sample | Bytes | Formulas | OK | CPU seconds | Peak MiB | Wall seconds |
|---|---:|---:|---|---:|---:|---:|
| small | 97646 | 1280 | yes | 0.6600 | 225.6 | 1.0137 |
| medium | 2026825 | 26790 | yes | 1.5300 | 277.6 | 1.7233 |
| large-5mb | 5083545 | 67010 | yes | 2.4600 | 308.3 | 2.5387 |
| large-25mb | 25723945 | 335190 | yes | 12.1800 | 858.4 | 10.4446 |

Every recalculation finished inside the product time limit.
