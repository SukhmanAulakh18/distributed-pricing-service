# Stage 1: compile the C++ library and pybind11 module into a wheel.
FROM python:3.12-slim AS build
RUN apt-get update && apt-get install -y --no-install-recommends g++ && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY pyproject.toml ./
COPY core ./core
RUN pip wheel --no-cache-dir --no-deps -w /wheels .

# Stage 2: slim runtime with only the wheel and the service code.
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir --no-deps /wheels/*.whl && rm -rf /wheels
COPY service ./service
RUN useradd -r -u 10001 app
USER 10001
EXPOSE 8000
CMD ["uvicorn", "service.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]
