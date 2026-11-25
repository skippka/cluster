import math
import random
import time

random.seed(7)
samples = 800_000
inside = 0
t0 = time.perf_counter()

for _ in range(samples):
    x = random.random()
    y = random.random()
    if x * x + y * y <= 1.0:
        inside += 1

pi_est = 4.0 * inside / samples
dt = time.perf_counter() - t0
err = abs(math.pi - pi_est)
print(f"Monte Carlo Pi ({samples:,} points): {pi_est:.6f}")
print(f"Error vs math.pi: {err:.6f} | Time: {dt:.3f}s")
