function validate(x) {
    if (!x) {
        throw new Error("invalid");
    }
    return x;
}
validate(1);
