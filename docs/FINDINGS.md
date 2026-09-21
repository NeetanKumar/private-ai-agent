# Findings: First Rented-GPU Test

One test session on a rented single RTX 4090 (24 GB) from a marketplace provider, running the 9B
model, with fake data only. Times come from the provider console and from timestamps in the
terminal. Where a time was not recorded it says so.

## Time taken

| Step | Time |
|---|---|
| Rent to running | about 10 minutes (console showed 6m17s still starting, 9m56s running). The console warned that VMs on hosts with lots of memory can take 30 minutes or more. |
| First login to GPU visible on the host | about 3 minutes (login 16:22, host GPU check 16:25 UTC) |
| Install the container toolkit and see the GPU from inside Docker | about 3.5 minutes (16:25 to 16:28 UTC) |
| Clone, build and start the stack | not timed |
| Pull the 9B and embedding models | not timed; finished quickly on a link of about 1.7 Gbps |
| Long reasoning question, 9B, warm | 29 seconds |
| Same class of question, 4B on a Mac, cold / warm | 90 seconds cold; about 2 seconds warm for a short reply |

A smooth run is roughly 30 to 40 minutes end to end. Plan a buffer of 60 to 90 minutes before a demo.

## What the test verified

- A real model answers through the gateway on a real GPU, on the private lane.
- The UI works against it over an SSH tunnel.
- Docker can use the GPU after the container toolkit is installed.

## What went wrong

- **`make models` failed on a fresh server.** The model server sits on a network with no route out, so
  it could not download models (DNS lookup failed). Worked around with a one-off container that has
  internet and shares the model volume. The same fault affects the CPU variant.
- **The container toolkit is not preinstalled** on the marketplace VM image. Docker was present but
  `--gpus all` failed until it was installed.
- **A terminal tunnel interleaved warnings with typed commands,** breaking a pasted block. Open the
  tunnel in a separate terminal.
- **The disk suggestion differed from the estimate.** The provider suggested 130 GB for its VM
  template; the estimate of 50 GB ignored the template's own base image.
- **The model names in the repository were not what the test needed,** so a `sed` edit was required
  on the server.

## Provider notes

- Two of the three providers tried first gated GPUs behind extra verification or a deposit for a new account.
- Stopping an instance does not stop billing on every provider. Storage alone was shown at roughly
  $0.053 per hour for 130 GB, which is about $38 a month, so keeping a stopped instance costs more
  than rebuilding it.
- The card is not reserved once an instance is stopped, so a restart may not be possible.

## Not tested

The outside port scan, the packet-capture reconciliation, the host firewall script, the frontier
lane with a real key, the tool-use score, and retrieval quality with the real embedding model.
