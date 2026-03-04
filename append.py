import sys

def main():
    if len(sys.argv) < 2:
        return
    source_file = sys.argv[1]
    with open("CUA_CODE_EXPLANATION.txt", "a", encoding="utf-8") as target:
        with open(source_file, "r", encoding="utf-8") as source:
            target.write(source.read() + "\n")

if __name__ == "__main__":
    main()
