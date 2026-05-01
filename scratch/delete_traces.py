import requests

project_id = "UHJvamVjdDoy"
base_url = "http://localhost:6006/v1"


def delete_all_traces():
    # List traces
    resp = requests.get(f"{base_url}/projects/{project_id}/traces?limit=100")
    resp.raise_for_status()
    traces = resp.json().get("data", [])

    print(f"Found {len(traces)} traces to delete.")

    for trace in traces:
        trace_id = trace["id"]
        print(f"Deleting trace {trace_id}...")
        del_resp = requests.delete(f"{base_url}/traces/{trace_id}")
        del_resp.raise_for_status()

    print("All traces deleted.")


if __name__ == "__main__":
    delete_all_traces()
