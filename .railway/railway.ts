import { defineRailway, preserve, project, service, volume } from "railway/iac";

// One API replica. The week job and its status live in process memory.
// Railpack installs the package from requirements.txt. The volume covers
// data/raw and data/runs, which are gitignored.
export default defineRailway(() => {
  const runs = volume("pulse-data", {
    sizeMB: 1024,
  });

  const api = service("api", {
    start: "python -m groww_pulse serve --host 0.0.0.0 --port $PORT",
    healthcheck: "/api/weeks",
    healthcheckTimeout: 120,
    replicas: 1,
    volumeMounts: {
      "/app/data": runs,
    },
    env: {
      RAILPACK_PYTHON_VERSION: "3.12",
      ANTHROPIC_API_KEY: preserve(),
      PULSE_MCP_TOKEN: preserve(),
      PULSE_API_TOKEN: preserve(),
    },
  });

  return project("groww-pulse", {
    resources: [api, runs],
  });
});
