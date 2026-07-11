fn main() {
    let mut v = vec![1, 2];
    {
        let a = &mut v;
        a.push(3);
    }
    let b = &mut v;
    b.push(4);
}
