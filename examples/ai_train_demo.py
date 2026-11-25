"""Example AI script for Cluster LAN: training a simple linear model via gradient descent."""

import math
import random
import time

random.seed(42)

data = [(x / 10.0, 2.5 * (x / 10.0) - 1.2 + random.uniform(-0.05, 0.05)) for x in range(-20, 21)]

w = 0.0
b = 0.0
lr = 0.05
epochs = 200

t0 = time.perf_counter()
for epoch in range(1, epochs + 1):
    dw = 0.0
    db = 0.0
    loss = 0.0
    for x, y_true in data:
        y_pred = w * x + b
        err = y_pred - y_true
        loss += err * err
        dw += 2 * err * x
        db += 2 * err
    n = len(data)
    w -= lr * (dw / n)
    b -= lr * (db / n)
    if epoch in (1, 50, 100, 200):
        rmse = math.sqrt(loss / n)
        print(f"Epoch {epoch:3d} | RMSE={rmse:.4f} | w={w:.3f}, b={b:.3f}")

dt = time.perf_counter() - t0
print(f"Training completed in {dt:.3f}s. Final formula: y = {w:.2f}*x + ({b:.2f})")
