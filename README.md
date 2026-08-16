# Wi-Fi Walker

**Wi-Fi Walker is an experimental RSSI-based movement and distance-estimation system.**

It attempts to infer physical movement from changes in Wi-Fi signal strength, turning noisy RSSI measurements into a continuously updated movement model.

The project is primarily an experiment in **signal processing, measurement, verification, and state estimation** rather than a claim that Wi-Fi RSSI can provide precise positioning in arbitrary environments.

---

## What is this?

Wi-Fi devices constantly expose a measurable signal-strength value known as **RSSI (Received Signal Strength Indicator)**.

As a device moves relative to an access point, RSSI can change.

The basic idea behind Wi-Fi Walker is:

```text
Wi-Fi signal
     ↓
RSSI measurements
     ↓
RSSI → normalized model
     ↓
movement observations
     ↓
verification batches
     ↓
estimated movement
```

The interesting part is not simply reading RSSI.

RSSI is noisy.

A single measurement can change because of interference, reflections, people moving around, antenna orientation, or normal wireless fluctuations.

So Wi-Fi Walker treats measurements as **observations of a changing system**, rather than blindly converting every RSSI change into movement.

---

# Core Model

The current experimental model uses:

> **1 percentage point of the RSSI-derived model ≈ 0.1 meter**

This is an experimental calibration assumption, not a universal physical law.

The system converts RSSI changes into a normalized percentage-style representation and then interprets meaningful changes as movement observations.

Conceptually:

```text
RSSI
 │
 ▼
Normalize
 │
 ▼
Model value
 │
 ├── meaningful change → observation
 │
 └── insignificant change → ignored
```

---

# Observation System

One of the main design goals is preventing individual noisy measurements from immediately becoming trusted movement.

Every meaningful model change becomes an **observation**.

Observations are stored in a FIFO queue:

```text
oldest
  ↓
[ observation ]
[ observation ]
[ observation ]
  ↓
newest
```

Every **3 observations** form a verification batch.

```text
Observation 1 ─┐
Observation 2 ─┼──→ Verification Batch
Observation 3 ─┘
```

If new observations arrive while a batch is being processed, they remain in the FIFO and are used for the next batch.

This means verification does not destroy information arriving during processing.

---

# Why Batches?

RSSI is inherently noisy.

Suppose the measurements produce:

```text
A → B → A → B → A
```

A naive system could interpret every fluctuation as movement.

Instead, Wi-Fi Walker asks:

> "Does this sequence of observations provide enough evidence that the underlying state actually changed?"

This makes the system closer to a small **measurement + verification pipeline** than a simple RSSI-to-distance formula.

---

# Architecture

The system can be thought of as several stages:

```text
┌─────────────────────┐
│   Wi-Fi Interface   │
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│    RSSI Sampling    │
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│ Normalization /     │
│ Model Conversion    │
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│ Change Detection    │
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│   FIFO Observations │
└──────────┬──────────┘
           │
        3 samples
           │
           ▼
┌─────────────────────┐
│ Verification Batch  │
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│ Movement Estimate   │
└─────────────────────┘
```

---

# Design Principles

### 1. Measurements are not truth

RSSI is treated as noisy sensor data.

The system separates:

```text
measurement
    ≠
observation
    ≠
verified movement
```

This distinction is important.

---

### 2. Preserve incoming data

The observation queue is FIFO.

When a verification batch is being processed, incoming observations are **not discarded**.

They remain available for subsequent processing.

---

### 3. Verify before trusting

A single unusual measurement should not automatically cause the system's estimated position to jump.

The verification stage provides a buffer between raw observations and the movement model.

---

### 4. Keep the model explicit

The distance/movement relationship is represented explicitly instead of hiding it inside a complicated black-box system.

That makes the experiment easier to inspect, modify, and test.

---

# Example

A simplified run might look like:

```text
RSSI samples
     ↓
-54
-55
-56
-56
-58
-59
     ↓
normalized model
     ↓
meaningful changes detected
     ↓
observations
     ↓
[ O1, O2, O3 ]
     ↓
verification
     ↓
movement update
```

Meanwhile, if additional measurements arrive:

```text
[ O4, O5 ]
```

they remain queued instead of being lost.

The next verification batch can therefore be:

```text
[ O4, O5, O6 ]
```

---

# Why This Project Exists

Wi-Fi Walker is an experiment in answering a broader engineering question:

> **How much useful information can be extracted from a cheap, noisy signal when the measurement system itself is carefully designed?**

The project explores concepts including:

* RSSI measurement
* signal noise
* normalization
* thresholding
* state estimation
* FIFO data structures
* observation batching
* verification
* incremental models
* sensor uncertainty
* real-time processing

---

# Limitations

This project should **not** be interpreted as a general-purpose indoor GPS.

RSSI is affected by many environmental factors, including:

* walls
* furniture
* reflections
* interference
* device orientation
* antenna characteristics
* access-point placement
* other wireless devices
* people moving through the environment

Therefore, the relationship between RSSI and physical distance can vary significantly between environments.

The current model is experimental and requires calibration/testing for meaningful results.

---

# Experimental Nature

The goal is not to pretend that the current model is perfect.

The goal is to create a system where assumptions can be:

```text
defined
  ↓
implemented
  ↓
measured
  ↓
verified
  ↓
challenged
  ↓
improved
```

Future versions can replace individual components without changing the overall measurement pipeline.

---

# Future Experiments

Potential directions include:

* adaptive RSSI filtering
* environment-specific calibration
* multiple access points
* confidence scores
* statistical filtering
* Kalman-style state estimation
* temporal smoothing
* movement-direction estimation
* automatic calibration
* uncertainty bounds
* comparison against physical measurements
* multi-device experiments
* visualization of the estimated movement path

---

# Running

Clone the repository:

```bash
git clone <repository-url>
cd wifi-walker
```

Install the project's dependencies:

```bash
pip install -r requirements.txt
```

Then run:

```bash
python wifi_walker.py
```

> Replace the commands above with the actual entry point/dependency instructions for the current version of the project.

---

# Status

**Experimental / Research Project**

Wi-Fi Walker is an evolving experiment.

The current implementation is primarily useful for exploring the relationship between **Wi-Fi RSSI, noisy measurements, and inferred movement**.

Results should be treated as experimental measurements rather than guaranteed real-world positioning accuracy.

---

## Author

Built as an independent systems/engineering experiment.

The project is intentionally open to modification, testing, and alternative models.

**If you experiment with it, measure it.**
