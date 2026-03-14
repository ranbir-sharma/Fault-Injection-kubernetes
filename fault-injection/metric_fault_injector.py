import random
import subprocess
from flask import Flask, jsonify

app = Flask(__name__)


def inject_fault(metric):

    if random.random() < 0.2:   # 20% fault rate
        mode = random.choice(["spike", "drop"])

        if mode == "spike":
            return metric * 3

        if mode == "drop":
            return metric * 0.3

    return metric


def get_cpu_usage():

    cmd = ["kubectl", "top", "pods", "-n", "teastore", "--no-headers"]
    output = subprocess.check_output(cmd).decode()

    cpu_str = output.split()[1]  # example: 120m
    cpu = float(cpu_str.replace("m", ""))

    return cpu


@app.route("/metric")
def metric():

    real_cpu = get_cpu_usage()
    faulty_cpu = inject_fault(real_cpu)

    return jsonify({
        "real_cpu": real_cpu,
        "faulty_cpu": faulty_cpu
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001)