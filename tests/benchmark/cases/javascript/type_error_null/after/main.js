function userName(user) {
    return user?.name?.toUpperCase() ?? "ANON";
}
console.log(userName(null));
