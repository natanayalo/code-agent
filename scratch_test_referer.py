referer = "https://localhost:3000/path"
parts = referer.split("/")
print(f"Parts: {parts}")
origin = f"{parts[0]}//{parts[2]}"
print(f"Origin: {origin}")
join_origin = "/".join(parts[:3])
print(f"Join Origin: {join_origin}")
