N, M = map(int, input().split())
arr = [[] for _ in range(N + 1)]
for i in range(M):
    u, v = map(int, input().split())
    arr[u].append(v)
    arr[v].append(u)
color = [0] * (N + 1)
flag = True
for i in range(1, N + 1):
    if color[i] == 0:
        stack = [i]
        color[i] = 1
        while stack:
            v = stack.pop()
            for val in arr[v]:
                if color[val] == 0:
                    color[val] = 3 - color[v]
                    stack.append(val)
                elif color[val] == color[v]:
                    flag = False
                    break
            if not flag:
                break
    if not flag:
        break

if flag:
    print("Yes")
else:
    print("No")