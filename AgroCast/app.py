import sys


def main(argv=None):
    from agrocast.serve.cli import main as serve

    return serve(["serve", "--port", "8501", *(sys.argv[1:] if argv is None else argv)])


if __name__ == "__main__":
    main()
