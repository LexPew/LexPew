#!/usr/bin/env python3
"""
GitHub Stats SVG Generator (reconfigured for your GitHub profile repo)

What this script does:
- Uses GitHub GraphQL API to compute:
  - total contributions (commits via contribution calendar)
  - total stars across owned repos
  - total owned repos + total contributed repos (by affiliation)
  - followers count
  - LOC-ish stats by summing additions/deletions for commits authored by you across repos you have access to
- Updates SVG files by replacing text in elements with IDs:
  commit_data, star_data, repo_data, contrib_data, follower_data, loc_data, loc_add, loc_del, age_data
  and their *_dots spacer IDs.

import datetime
import hashlib
import os
import time
from pathlib import Path

import requests
from dateutil import relativedelta
from lxml import etree

# -----------------------------
# Config / Env
# -----------------------------

TOKEN = os.getenv("ACCESS_TOKEN") or os.getenv("GITHUB_TOKEN")
if not TOKEN:
    raise RuntimeError("Missing ACCESS_TOKEN (PAT) or GITHUB_TOKEN in env.")

USER_NAME = os.getenv("USER_NAME")
if not USER_NAME:
    raise RuntimeError("Missing USER_NAME in env (e.g. 'lexpew').")

HEADERS = {"Authorization": f"Bearer {TOKEN}"}

BIRTHDAY_STR = os.getenv("BIRTHDAY", "2004-09-04")
DARK_SVG = os.getenv("DARK_SVG", "dark_mode.svg")
LIGHT_SVG = os.getenv("LIGHT_SVG", "light_mode.svg")

CACHE_COMMENT_LINES = int(os.getenv("CACHE_COMMENT_LINES", "0"))
FORCE_CACHE = os.getenv("FORCE_CACHE", "0") == "1"

QUERY_COUNT = {
    "user_getter": 0,
    "follower_getter": 0,
    "graph_repos_stars": 0,
    "recursive_loc": 0,
    "graph_commits": 0,
    "loc_query": 0,
}

API_URL = "https://api.github.com/graphql"

# Create cache directory (Actions runs in a clean workspace)
Path("cache").mkdir(parents=True, exist_ok=True)

# Will be set in main()
OWNER_ID = None


# -----------------------------
# Helpers
# -----------------------------

def query_count(funct_id: str) -> None:
    global QUERY_COUNT
    QUERY_COUNT[funct_id] += 1


def format_plural(unit: int) -> str:
    return "s" if unit != 1 else ""


def daily_readme(birthday: datetime.datetime) -> str:
    """Returns the length of time since birthday, as 'X years, Y months, Z days' (+ 🎂 on birthday)."""
    diff = relativedelta.relativedelta(datetime.datetime.today(), birthday)
    return "{} {}, {} {}, {} {}{}".format(
        diff.years, "year" + format_plural(diff.years),
        diff.months, "month" + format_plural(diff.months),
        diff.days, "day" + format_plural(diff.days),
        " 🎂" if (diff.months == 0 and diff.days == 0) else "",
    )


def simple_request(func_name: str, query: str, variables: dict) -> requests.Response:
    """Returns a request, or raises an Exception if the response does not succeed."""
    request = requests.post(API_URL, json={"query": query, "variables": variables}, headers=HEADERS, timeout=60)
    if request.status_code == 200:
        return request
    raise Exception(func_name, "has failed with", request.status_code, request.text, QUERY_COUNT)


def perf_counter(funct, *args):
    """Returns (function_result, elapsed_seconds)."""
    start = time.perf_counter()
    funct_return = funct(*args)
    return funct_return, time.perf_counter() - start


def formatter(query_type: str, difference: float, funct_return=False, whitespace: int = 0):
    """Print a formatted time differential; optionally return formatted value padded to whitespace."""
    print("{:<23}".format("   " + query_type + ":"), sep="", end="")
    if difference > 1:
        print("{:>12}".format(f"{difference:.4f} s "))
    else:
        print("{:>12}".format(f"{difference*1000:.4f} ms"))
    if whitespace:
        return f"{'{:,}'.format(funct_return): <{whitespace}}"
    return funct_return


# -----------------------------
# GitHub GraphQL queries
# -----------------------------

def user_getter(username: str):
    """Returns the account ID and creation time of the user."""
    query_count("user_getter")
    query = """
    query($login: String!){
        user(login: $login) {
            id
            createdAt
        }
    }"""
    variables = {"login": username}
    request = simple_request(user_getter.__name__, query, variables)
    return {"id": request.json()["data"]["user"]["id"]}, request.json()["data"]["user"]["createdAt"]


def follower_getter(username: str) -> int:
    """Returns the number of followers of the user."""
    query_count("follower_getter")
    query = """
    query($login: String!){
        user(login: $login) {
            followers {
                totalCount
            }
        }
    }"""
    request = simple_request(follower_getter.__name__, query, {"login": username})
    return int(request.json()["data"]["user"]["followers"]["totalCount"])


def graph_commits(start_date: str, end_date: str) -> int:
    """Uses GitHub GraphQL v4 API to return total contributions between dates (calendar contributions)."""
    query_count("graph_commits")
    query = """
    query($start_date: DateTime!, $end_date: DateTime!, $login: String!) {
        user(login: $login) {
            contributionsCollection(from: $start_date, to: $end_date) {
                contributionCalendar {
                    totalContributions
                }
            }
        }
    }"""
    variables = {"start_date": start_date, "end_date": end_date, "login": USER_NAME}
    request = simple_request(graph_commits.__name__, query, variables)
    return int(request.json()["data"]["user"]["contributionsCollection"]["contributionCalendar"]["totalContributions"])


def stars_counter(edges) -> int:
    """Count total stars in repositories."""
    total_stars = 0
    for node in edges:
        total_stars += node["node"]["stargazers"]["totalCount"]
    return total_stars


def graph_repos_stars(count_type: str, owner_affiliation, cursor=None):
    """Return repo totalCount or total stars across repos for given affiliation(s)."""
    query_count("graph_repos_stars")
    query = """
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 100, after: $cursor, ownerAffiliations: $owner_affiliation) {
                totalCount
                edges {
                    node {
                        ... on Repository {
                            nameWithOwner
                            stargazers { totalCount }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }"""
    variables = {"owner_affiliation": owner_affiliation, "login": USER_NAME, "cursor": cursor}
    request = simple_request(graph_repos_stars.__name__, query, variables)

    repos = request.json()["data"]["user"]["repositories"]
    if count_type == "repos":
        return repos["totalCount"]
    elif count_type == "stars":
        # Need pagination to sum all stars if >100 repos
        total = stars_counter(repos["edges"])
        while repos["pageInfo"]["hasNextPage"]:
            cursor = repos["pageInfo"]["endCursor"]
            variables["cursor"] = cursor
            request = simple_request(graph_repos_stars.__name__, query, variables)
            repos = request.json()["data"]["user"]["repositories"]
            total += stars_counter(repos["edges"])
        return total
    else:
        raise ValueError("count_type must be 'repos' or 'stars'")


# -----------------------------
# LOC (additions/deletions) cache system
# -----------------------------

def cache_filename() -> str:
    """Unique cache filename per USER_NAME."""
    return "cache/" + hashlib.sha256(USER_NAME.encode("utf-8")).hexdigest() + ".txt"


def force_close_file(data, cache_comment):
    """Preserve whatever data was written to cache before crash."""
    filename = cache_filename()
    with open(filename, "w", encoding="utf-8") as f:
        f.writelines(cache_comment)
        f.writelines(data)
    print("There was an error while writing to the cache file. Partial data saved to", filename)


def flush_cache(edges, filename, comment_size):
    """Wipes the cache file; preserves comment block if any."""
    with open(filename, "r", encoding="utf-8") as f:
        data = []
        if comment_size > 0:
            data = f.readlines()[:comment_size]
    with open(filename, "w", encoding="utf-8") as f:
        f.writelines(data)
        for node in edges:
            f.write(hashlib.sha256(node["node"]["nameWithOwner"].encode("utf-8")).hexdigest() + " 0 0 0 0\n")


def loc_counter_one_repo(owner, repo_name, data, cache_comment, history, addition_total, deletion_total, my_commits):
    """Accumulate additions/deletions for commits authored by you; paginate via recursive_loc()."""
    global OWNER_ID
    for node in history["edges"]:
        author_user = node["node"]["author"]["user"] if node["node"]["author"] else None
        if author_user and author_user.get("id") == OWNER_ID.get("id"):
            my_commits += 1
            addition_total += node["node"]["additions"]
            deletion_total += node["node"]["deletions"]

    if history["edges"] == [] or not history["pageInfo"]["hasNextPage"]:
        return addition_total, deletion_total, my_commits
    return recursive_loc(
        owner,
        repo_name,
        data,
        cache_comment,
        addition_total,
        deletion_total,
        my_commits,
        history["pageInfo"]["endCursor"],
    )


def recursive_loc(owner, repo_name, data, cache_comment, addition_total=0, deletion_total=0, my_commits=0, cursor=None):
    """Fetch commit history (100 at a time) and sum additions/deletions for your authored commits."""
    query_count("recursive_loc")
    query = """
    query ($repo_name: String!, $owner: String!, $cursor: String) {
        repository(name: $repo_name, owner: $owner) {
            defaultBranchRef {
                target {
                    ... on Commit {
                        history(first: 100, after: $cursor) {
                            totalCount
                            edges {
                                node {
                                    ... on Commit {
                                        committedDate
                                        deletions
                                        additions
                                        author {
                                            user { id }
                                        }
                                    }
                                }
                            }
                            pageInfo {
                                endCursor
                                hasNextPage
                            }
                        }
                    }
                }
            }
        }
    }"""
    variables = {"repo_name": repo_name, "owner": owner, "cursor": cursor}

    # Can't use simple_request because we want to save cache before raising if failure
    request = requests.post(API_URL, json={"query": query, "variables": variables}, headers=HEADERS, timeout=60)

    if request.status_code == 200:
        repo = request.json()["data"]["repository"]
        if repo and repo["defaultBranchRef"] is not None:
            history = repo["defaultBranchRef"]["target"]["history"]
            return loc_counter_one_repo(owner, repo_name, data, cache_comment, history, addition_total, deletion_total, my_commits)
        return (0, 0, 0)  # empty repo / no default branch

    force_close_file(data, cache_comment)
    if request.status_code == 403:
        raise Exception("Too many requests in a short amount of time! You've hit GitHub's anti-abuse limit.")
    raise Exception("recursive_loc() has failed with", request.status_code, request.text, QUERY_COUNT)


def loc_query(owner_affiliation, comment_size=0, force_cache=False, cursor=None, edges=None):
    """
    Query all repos you have access to (by affiliation), 60 at a time (avoid timeouts).
    Returns [added_loc, deleted_loc, net_loc, cached_bool].
    """
    if edges is None:
        edges = []

    query_count("loc_query")
    query = """
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 60, after: $cursor, ownerAffiliations: $owner_affiliation) {
                edges {
                    node {
                        ... on Repository {
                            nameWithOwner
                            defaultBranchRef {
                                target {
                                    ... on Commit {
                                        history { totalCount }
                                    }
                                }
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }"""
    variables = {"owner_affiliation": owner_affiliation, "login": USER_NAME, "cursor": cursor}
    request = simple_request(loc_query.__name__, query, variables)
    repos = request.json()["data"]["user"]["repositories"]

    edges += repos["edges"]
    if repos["pageInfo"]["hasNextPage"]:
        return loc_query(owner_affiliation, comment_size, force_cache, repos["pageInfo"]["endCursor"], edges)

    return cache_builder(edges, comment_size, force_cache)


def cache_builder(edges, comment_size, force_cache, loc_add=0, loc_del=0):
    """Check each repo's commit count; if changed, update its LOC via recursive_loc()."""
    cached = True
    filename = cache_filename()

    try:
        with open(filename, "r", encoding="utf-8") as f:
            data = f.readlines()
    except FileNotFoundError:
        data = []
        if comment_size > 0:
            for _ in range(comment_size):
                data.append("This line is a comment block. Write whatever you want here.\n")
        with open(filename, "w", encoding="utf-8") as f:
            f.writelines(data)

    # If repos changed count or forced, rebuild cache skeleton
    if (len(data) - comment_size) != len(edges) or force_cache:
        cached = False
        flush_cache(edges, filename, comment_size)
        with open(filename, "r", encoding="utf-8") as f:
            data = f.readlines()

    cache_comment = data[:comment_size]
    data = data[comment_size:]

    for index in range(len(edges)):
        repo_line = data[index].strip().split()
        repo_hash = repo_line[0] if repo_line else ""

        expected_hash = hashlib.sha256(edges[index]["node"]["nameWithOwner"].encode("utf-8")).hexdigest()

        if repo_hash == expected_hash:
            try:
                # Some repos can have defaultBranchRef None (empty)
                default_ref = edges[index]["node"]["defaultBranchRef"]
                if default_ref is None:
                    data[index] = f"{expected_hash} 0 0 0 0\n"
                    continue

                total_commits = int(default_ref["target"]["history"]["totalCount"])
                cached_commit_count = int(repo_line[1])

                if cached_commit_count != total_commits:
                    owner, repo_name = edges[index]["node"]["nameWithOwner"].split("/")
                    loc = recursive_loc(owner, repo_name, data, cache_comment)
                    # repo_hash total_commits my_commits additions deletions
                    data[index] = f"{expected_hash} {total_commits} {loc[2]} {loc[0]} {loc[1]}\n"
            except (TypeError, ValueError, IndexError):
                data[index] = f"{expected_hash} 0 0 0 0\n"
        else:
            # Hash mismatch (repo order changed?) rebuild this line
            cached = False
            data[index] = f"{expected_hash} 0 0 0 0\n"

    with open(filename, "w", encoding="utf-8") as f:
        f.writelines(cache_comment)
        f.writelines(data)

    for line in data:
        parts = line.split()
        if len(parts) >= 5:
            loc_add += int(parts[3])
            loc_del += int(parts[4])

    return [loc_add, loc_del, loc_add - loc_del, cached]


def commit_counter(comment_size: int) -> int:
    """Counts your authored commits across repos using cache file."""
    total_commits = 0
    filename = cache_filename()
    with open(filename, "r", encoding="utf-8") as f:
        data = f.readlines()
    data = data[comment_size:]
    for line in data:
        parts = line.split()
        if len(parts) >= 3:
            total_commits += int(parts[2])
    return total_commits


# -----------------------------
# SVG rewriting
# -----------------------------

def find_and_replace(root, element_id: str, new_text: str) -> None:
    element = root.find(f".//*[@id='{element_id}']")
    if element is not None:
        element.text = new_text


def justify_format(root, element_id: str, new_text, length: int = 0) -> None:
    """
    Updates element text and updates *_dots spacer to keep monospaced alignment.
    length is a "target spacing budget" used by your template.
    """
    if isinstance(new_text, int):
        new_text = f"{new_text:,}"
    new_text = str(new_text)

    find_and_replace(root, element_id, new_text)

    just_len = max(0, length - len(new_text))
    if just_len <= 2:
        dot_map = {0: "", 1: " ", 2: ". "}
        dot_string = dot_map[just_len]
    else:
        dot_string = " " + ("." * just_len) + " "

    find_and_replace(root, f"{element_id}_dots", dot_string)


def svg_overwrite(filename: str, age_data, commit_data, star_data, repo_data, contrib_data, follower_data, loc_data):
    """Parse SVG and update elements."""
    tree = etree.parse(filename)
    root = tree.getroot()

    # These "length" values are tuned to your template layout.
    justify_format(root, "commit_data", commit_data, 22)
    justify_format(root, "star_data", star_data, 14)
    justify_format(root, "repo_data", repo_data, 6)
    justify_format(root, "contrib_data", contrib_data)
    justify_format(root, "follower_data", follower_data, 10)
    justify_format(root, "loc_data", loc_data[2], 9)
    justify_format(root, "loc_add", loc_data[0])
    justify_format(root, "loc_del", loc_data[1], 7)
    justify_format(root, "age_data", age_data, 22)

    tree.write(filename, encoding="utf-8", xml_declaration=True)


# -----------------------------
# Main
# -----------------------------

def parse_birthday(bday_str: str) -> datetime.datetime:
    y, m, d = map(int, bday_str.split("-"))
    return datetime.datetime(y, m, d)


if __name__ == "__main__":
    print("Calculation times:")

    # Define global OWNER_ID (your user id) and account creation date
    user_data, user_time = perf_counter(user_getter, USER_NAME)
    OWNER_ID, acc_date = user_data
    formatter("account data", user_time)

    # Age data
    birthday_dt = parse_birthday(BIRTHDAY_STR)
    age_data, age_time = perf_counter(daily_readme, birthday_dt)
    formatter("age calculation", age_time)

    # LOC data (cached)
    total_loc, loc_time = perf_counter(
        loc_query,
        ["OWNER", "COLLABORATOR", "ORGANIZATION_MEMBER"],
        CACHE_COMMENT_LINES,
        FORCE_CACHE,
    )
    formatter("LOC (cached)", loc_time) if total_loc[-1] else formatter("LOC (no cache)", loc_time)

    # Commits (from cache), stars, repos, contributed repos, followers
    commit_data, commit_time = perf_counter(commit_counter, CACHE_COMMENT_LINES)
    formatter("commit count", commit_time)

    star_data, star_time = perf_counter(graph_repos_stars, "stars", ["OWNER"])
    formatter("stars", star_time)

    repo_data, repo_time = perf_counter(graph_repos_stars, "repos", ["OWNER"])
    formatter("owned repos", repo_time)

    contrib_data, contrib_time = perf_counter(graph_repos_stars, "repos", ["OWNER", "COLLABORATOR", "ORGANIZATION_MEMBER"])
    formatter("contrib repos", contrib_time)

    follower_data, follower_time = perf_counter(follower_getter, USER_NAME)
    formatter("followers", follower_time)

    # Format LOC numbers as strings with commas for SVG (keep same output style)
    loc_formatted = [f"{total_loc[0]:,}", f"{total_loc[1]:,}", f"{total_loc[2]:,}"]

    # Overwrite SVGs
    svg_overwrite(DARK_SVG, age_data, commit_data, star_data, repo_data, contrib_data, follower_data, loc_formatted)
    svg_overwrite(LIGHT_SVG, age_data, commit_data, star_data, repo_data, contrib_data, follower_data, loc_formatted)

    # Total time summary
    total_time = user_time + age_time + loc_time + commit_time + star_time + repo_time + contrib_time + follower_time
    print("Total function time:", f"{total_time:.4f}", "s")

    print("Total GitHub GraphQL API calls:", f"{sum(QUERY_COUNT.values()):>3}")
    for funct_name, count in QUERY_COUNT.items():
        print(f"{('   ' + funct_name + ':'):<28}", f"{count:>6}")