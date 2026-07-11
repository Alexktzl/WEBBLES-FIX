fn main() {
    let mut v = vec![1, 2];
    let a = &mut v;
    let b = &mut v;
    a.push(3);
    b.push(4);
}
