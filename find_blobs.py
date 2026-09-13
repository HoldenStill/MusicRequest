import subprocess
import operator

# Get all objects and their sizes
out = subprocess.check_output(['git', 'rev-list', '--objects', '--all']).decode('utf-8').strip().split('\n')
objects = {}
for line in out:
    parts = line.split(maxsplit=1)
    if len(parts) == 2:
        objects[parts[0]] = parts[1]
    elif len(parts) == 1:
        objects[parts[0]] = ''

sizes = {}
# Batch check sizes
p = subprocess.Popen(['git', 'cat-file', '--batch-check'], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
input_data = '\n'.join(objects.keys()).encode('utf-8')
out, _ = p.communicate(input_data)

for line in out.decode('utf-8').strip().split('\n'):
    parts = line.split()
    if len(parts) == 3 and parts[1] == 'blob':
        sizes[parts[0]] = int(parts[2])

sorted_sizes = sorted(sizes.items(), key=operator.itemgetter(1), reverse=True)
for sha, size in sorted_sizes[:20]:
    print(f"{size/1024/1024:.2f} MB - {objects.get(sha, '')}")
